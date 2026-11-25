"""Exercise authentication through the real HTTP/WebSocket boundaries."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import time

from aiohttp import CookieJar, WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
import pytest

from echosense.auth import AuthStore, COOKIE
from echosense.server import ConsoleServer

PASSWORD = "a long test-only passphrase"
NEW_PASSWORD = "a different test-only passphrase"


async def make_client(tmp_path):
    app = SimpleNamespace(hello=lambda: {"type": "hello"}, browser_tap=SimpleNamespace(listeners=[]))
    server = ConsoleServer(app, "127.0.0.1", 0, auth_path=tmp_path / "accounts.sqlite3")
    client = TestClient(TestServer(server.web), cookie_jar=CookieJar(unsafe=True))
    await client.start_server()
    return server, client, str(client.make_url("/")).rstrip("/")


def test_auth_http_flow(tmp_path):
    async def scenario():
        server, client, origin = await make_client(tmp_path)
        try:
            assert (await client.get("/login")).status == 200
            for path in ("/", "/parents", "/static/parent.html", "/static/index.html", "/static/app.js"):
                result = await client.get(path, allow_redirects=False)
                assert result.status == 302
                assert result.headers["Location"] == "/login"
            for path in ("/api/state", "/api/perf"):
                assert (await client.get(path)).status == 401
            with pytest.raises(WSServerHandshakeError) as denied:
                await client.ws_connect("/ws", headers={"Origin": origin})
            assert denied.value.status == 401
            body = {"username": "parent", "password": PASSWORD, "code": server.auth.setup_code}
            # Account setup cannot be initiated from another site or without the device code.
            assert (await client.post("/api/auth/setup", json=body)).status == 403
            assert (await client.post("/api/auth/setup", headers={"Origin": "https://other.example"}, json=body)).status == 403
            bad = await client.post("/api/auth/setup", headers={"Origin": origin}, json={**body, "code": "wrong"})
            assert bad.status == 400
            assert not server.auth.has_account()
            response = await client.post("/api/auth/setup", headers={"Origin": origin}, json=body)
            assert response.status == 200
            cookie = response.cookies[COOKIE]
            assert cookie["httponly"] and cookie["samesite"] == "Strict"
            assert cookie["max-age"] == str(12 * 60 * 60)
            assert response.headers["Cache-Control"] == "no-store"
            assert (await client.get("/parents")).status == 200
            assert (await client.get("/")).status == 200
            assert (await client.post("/api/auth/setup", headers={"Origin": origin}, json=body)).status == 409
            status = await (await client.get("/api/auth/status")).json()
            assert status["username"] == "parent" and status["authenticated"]
            csrf = status["csrf"]
            assert (await client.post("/api/auth/logout", headers={"Origin": origin}, json={})).status == 403
            with pytest.raises(WSServerHandshakeError) as denied:
                await client.ws_connect("/ws", headers={"Origin": "https://other.example"})
            assert denied.value.status == 403
            ws = await client.ws_connect("/ws", headers={"Origin": origin})
            assert (await ws.receive_json())["type"] == "hello"
            token = cookie.value
            closing = asyncio.create_task(ws.receive())
            response = await client.post("/api/auth/logout", headers={"Origin": origin, "X-CSRF-Token": csrf}, json={})
            assert response.status == 200
            assert (await closing).type == WSMsgType.CLOSE
            assert ws.close_code == 4001
            assert server.auth.session(token) is None
            assert (await client.get("/parents", allow_redirects=False)).status == 302
            login = {"username": "parent", "password": "incorrect"}
            assert (await client.post("/api/auth/login", headers={"Origin": origin}, json=login)).status == 401
            login["password"] = PASSWORD
            response = await client.post("/api/auth/login", headers={"Origin": origin}, json=login)
            assert response.status == 200
            assert response.cookies[COOKIE].value != token
            status = await (await client.get("/api/auth/status")).json()
            headers = {"Origin": origin, "X-CSRF-Token": status["csrf"]}
            assert (await client.post("/api/auth/password", headers=headers,
                                     json={"current": "wrong", "password": NEW_PASSWORD})).status == 400
            other_token, _ = server.auth.create_session()
            response = await client.post("/api/auth/password", headers=headers,
                                         json={"current": PASSWORD, "password": NEW_PASSWORD})
            assert response.status == 200
            assert server.auth.session(other_token) is None
            assert not (await (await client.get("/api/auth/status")).json())["authenticated"]
            assert not server.auth.verify("parent", PASSWORD)
            assert server.auth.verify("parent", NEW_PASSWORD)
        finally:
            await client.close()
    asyncio.run(scenario())


def test_login_rate_limit_and_input_handling(tmp_path):
    async def scenario():
        server, client, origin = await make_client(tmp_path)
        try:
            headers = {"Origin": origin}
            assert (await client.post("/api/auth/login", headers=headers, json=[])).status == 400
            assert (await client.post("/api/auth/login", headers=headers, json={"password": 9})).status == 400
            assert (await client.post("/api/auth/login", headers=headers, json={"password": "x" * 129})).status == 400
            for _ in range(8):
                response = await client.post("/api/auth/login", headers=headers,
                                             json={"username": "nonexistent", "password": "test"})
                assert response.status == 401
            response = await client.post("/api/auth/login", headers=headers,
                                         json={"username": "another", "password": "test"})
            assert response.status == 429
            assert response.headers["Retry-After"] == "60"
        finally:
            await client.close()
    asyncio.run(scenario())


def test_password_storage_restart_expiry_and_local_recovery(tmp_path):
    path = tmp_path / "accounts.sqlite3"
    store = AuthStore(path)
    with pytest.raises(ValueError):
        store.create_owner("parent", "short", store.setup_code)
    store.create_owner("parent", PASSWORD, store.setup_code)
    token, _ = store.create_session()
    assert PASSWORD.encode() not in path.read_bytes()
    assert token.encode() not in path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    restarted = AuthStore(path)
    assert restarted.verify("parent", PASSWORD)
    assert restarted.session(token)["username"] == "parent"
    with restarted.connect() as db:
        db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    assert restarted.session(token) is None
    token, _ = restarted.create_session()
    restarted.reset_password(NEW_PASSWORD)
    assert restarted.session(token) is None
    assert not restarted.verify("parent", PASSWORD)
    assert restarted.verify("parent", NEW_PASSWORD)
    with pytest.raises(ValueError):
        restarted.create_owner("other", PASSWORD, restarted.setup_code)


def test_expired_websocket_cannot_send_commands(tmp_path):
    async def scenario():
        server, client, origin = await make_client(tmp_path)
        try:
            server.auth.create_owner("parent", PASSWORD, server.auth.setup_code)
            await client.post("/api/auth/login", headers={"Origin": origin},
                              json={"username": "parent", "password": PASSWORD})
            ws = await client.ws_connect("/ws", headers={"Origin": origin})
            await ws.receive_json()
            with server.auth.connect() as db:
                db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
            await ws.send_json({"t": "hello"})
            assert (await ws.receive()).type == WSMsgType.CLOSE
            assert ws.close_code == 4001
            assert (await client.get("/api/state")).status == 401
        finally:
            await client.close()
    asyncio.run(scenario())


def test_explicit_https_origin_sets_secure_cookie(tmp_path, monkeypatch):
    monkeypatch.setenv("ECHOSENSE_ORIGIN", "https://echo.example")
    async def scenario():
        server, client, origin = await make_client(tmp_path)
        try:
            server.auth.create_owner("parent", PASSWORD, server.auth.setup_code)
            credentials = {"username": "parent", "password": PASSWORD}
            assert (await client.post("/api/auth/login", headers={"Origin": origin}, json=credentials)).status == 403
            response = await client.post("/api/auth/login", headers={"Origin": "https://echo.example"}, json=credentials)
            assert response.status == 200
            assert response.cookies[COOKIE]["secure"]
        finally:
            await client.close()
    asyncio.run(scenario())


def test_terminal_password_recovery(tmp_path, monkeypatch):
    from echosense.__main__ import cmd_reset_password
    path = tmp_path / "accounts.sqlite3"
    store = AuthStore(path)
    store.create_owner("parent", PASSWORD, store.setup_code)
    token, _ = store.create_session()
    answers = iter([NEW_PASSWORD, NEW_PASSWORD])
    monkeypatch.setattr("getpass.getpass", lambda _: next(answers))
    cmd_reset_password(SimpleNamespace(config=None, profile=None, auth_db=str(path)))
    assert store.verify("parent", NEW_PASSWORD)
    assert store.session(token) is None
