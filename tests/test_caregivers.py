"""Caregiver invitation migration and enforced permissions at the network boundary."""
import asyncio
import hashlib
import sqlite3
from types import SimpleNamespace

from aiohttp import CookieJar, WSMsgType
from aiohttp.test_utils import TestClient, TestServer
import pytest

from echosense.auth import AuthStore, token_hash
from echosense.server import ConsoleServer
from tests.test_supervision import TrainingDevice

PASSWORD = "a test caregiver passphrase"


def test_upgrade_keeps_existing_owner_and_sessions(tmp_path):
    path = tmp_path / "old.sqlite3"
    from echosense.auth import password_hash
    salt = b"0123456789abcdef"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE account(id INTEGER PRIMARY KEY CHECK(id=1),username TEXT,salt BLOB,password BLOB);"
                         "CREATE TABLE sessions(digest TEXT PRIMARY KEY,csrf TEXT,expires REAL);")
        db.execute("INSERT INTO account VALUES(1,?,?,?)", ("parent", salt, password_hash(PASSWORD, salt)))
        db.execute("INSERT INTO sessions VALUES(?, 'csrf', 9999999999)", (token_hash("old-token"),))
    store = AuthStore(path)
    assert store.verify("parent", PASSWORD)
    assert store.session("old-token")["role"] == "owner"
    assert store.session("old-token")["user_id"] == 1
    assert AuthStore(path).session("old-token")["permissions"] == ["view", "train", "configure", "accounts"]


def test_caregiver_roles_invitation_reuse_revocation_and_training(tmp_path):
    async def scenario():
        device = TrainingDevice(tmp_path)
        device.browser_tap = SimpleNamespace(listeners=[])
        server = ConsoleServer(device, "127.0.0.1", 0, auth_path=tmp_path / "accounts.sqlite3")
        server.auth.create_owner("parent", PASSWORD, server.auth.setup_code)
        clients = []
        host = TestServer(server.web)
        await host.start_server()
        origin = str(host.make_url("/")).rstrip("/")
        async def client():
            value = TestClient(host, cookie_jar=CookieJar(unsafe=True))
            await value.start_server(); clients.append(value); return value
        try:
            owner = await client()
            await owner.post('/api/auth/login', headers={"Origin": origin}, json={"username": "parent", "password": PASSWORD})
            owner_status = await (await owner.get('/api/auth/status')).json()
            owner_headers = {"Origin": origin, "X-CSRF-Token": owner_status["csrf"]}
            members = {}
            for name, role in (("viewer", "viewer"), ("trainer", "trainer"), ("tech", "technician")):
                invitation = await (await owner.post('/api/caregivers/invite', headers=owner_headers,
                    json={"username": name, "role": role})).json()
                member = await client()
                accepted = await member.post('/api/auth/accept-invite', headers={"Origin": origin},
                    json={"username": name, "password": PASSWORD, "code": invitation["code"]})
                assert accepted.status == 200
                status = await (await member.get('/api/auth/status')).json()
                members[name] = member, status
                retry = await member.post('/api/auth/accept-invite', headers={"Origin": origin},
                    json={"username": name, "password": PASSWORD, "code": invitation["code"]})
                assert retry.status == 400
            viewer, viewer_status = members["viewer"]
            assert (await viewer.get('/parents')).status == 200
            assert (await viewer.get('/api/caregivers')).status == 403
            assert (await viewer.get('/api/training/history')).status == 200
            assert (await viewer.post('/api/fitting', headers={"Origin": origin,"X-CSRF-Token":viewer_status["csrf"]},json={})).status == 403
            ws = await viewer.ws_connect('/ws',headers={"Origin": origin}); await ws.receive_json()
            await ws.send_json({"t":"training","cmd":"start","request_id":"denied"})
            denied = await ws.receive_json()
            assert denied["kind"] == "error" and not device.trainer.active
            await ws.close()
            trainer, trainer_status = members["trainer"]
            training_ws = await trainer.ws_connect('/ws',headers={"Origin":origin}); await training_ws.receive_json()
            await training_ws.send_json({"t":"training","cmd":"start","request_id":"start"})
            ack = await training_ws.receive_json()
            assert ack["type"] == "training_ack" and device.trainer.active
            # Viewer/device-manager roles cannot train; the trainer cannot change device fitting.
            assert (await trainer.post('/api/fitting',headers={"Origin":origin,"X-CSRF-Token":trainer_status["csrf"]},json={})).status == 403
            closing = asyncio.create_task(training_ws.receive())
            response = await owner.post(f'/api/caregivers/{trainer_status["user_id"]}/access',
                                        headers=owner_headers,json={"disabled":"true"})
            assert response.status == 200
            message = await closing
            if message.type == WSMsgType.TEXT:  # queued supervision broadcast before close
                message = await training_ws.receive()
            assert message.type == WSMsgType.CLOSE
            assert not device.trainer.active
            assert not (await (await trainer.get('/api/auth/status')).json())["authenticated"]
            tech, tech_status = members["tech"]
            values = {"head_height":1.1,"ear_height":1.0,"mount_height":0.8,"mount_pitch":-10,"fitting_checked":True}
            # Paused practice does not block a configuration-capable caregiver.
            assert (await tech.post('/api/fitting',headers={"Origin":origin,"X-CSRF-Token":tech_status["csrf"]},json=values)).status == 200
            assert device.coverage()["head_height"] == 1.1
        finally:
            for value in clients:
                await value.session.close()
            await host.close()
    asyncio.run(scenario())


def test_calibration_cannot_apply_if_practice_starts_during_floor_fit(tmp_path):
    import threading
    async def scenario():
        device = TrainingDevice(tmp_path)
        device.browser_tap = SimpleNamespace(listeners=[])
        server = ConsoleServer(device, "127.0.0.1", 0, auth_path=tmp_path / "accounts.sqlite3")
        server.auth.create_owner("parent", PASSWORD, server.auth.setup_code)
        client = TestClient(TestServer(server.web), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        origin = str(client.make_url('/')).rstrip('/')
        started, release = threading.Event(), threading.Event()
        def calibrate():
            started.set()
            release.wait(3)
            return {"height": 0.8, "pitch": -10}
        device.processor.calibrate_floor = calibrate
        try:
            await client.post('/api/auth/login', headers={"Origin": origin}, json={"username":"parent","password":PASSWORD})
            status = await (await client.get('/api/auth/status')).json()
            headers = {"Origin":origin,"X-CSRF-Token":status["csrf"]}
            task = asyncio.create_task(client.post('/api/fitting/calibrate', headers=headers,json={}))
            assert await asyncio.to_thread(started.wait, 2)
            server.supervision.handle('tab1', {"user_id":1,"username":"parent","role":"owner"}, {"cmd":"start"})
            release.set()
            response = await task
            assert response.status == 409
            assert device.settings.s.mount_height is None
        finally:
            release.set()
            await client.close()
    asyncio.run(scenario())
