"""Developer / caregiver console: serves the 3D depth viewer and a WebSocket.

Binary frames sent to the browser::

    0x01 | pad(3) | u32 json_len | json (padded to 4 bytes) | uint16 depth mm (h*w)
    0x02 | pad(3) | int16 stereo PCM @ 24 kHz             (only when "listen" is on)

Works the same against the simulator, a real Kinect on a laptop, the Docker
virtual Orange Pi, and the real Orange Pi over Wi-Fi.
"""
from __future__ import annotations

import asyncio
import json
import hmac
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
import struct
import uuid
from contextlib import suppress

from aiohttp import WSMsgType, web

from .app import EchoSense
from .config import WEB_DIR
from .auth import AuthStore, COOKIE, SESSION_SECONDS, ROLE_PERMISSIONS
from .device.supervision import SupervisedTraining, number
from .sensors.presets import coverage


def encode_frame(packet: dict) -> bytes:
    js = json.dumps(packet["state"], separators=(",", ":")).encode()
    js += b" " * ((-len(js)) % 4)
    return b"\x01\x00\x00\x00" + struct.pack("<I", len(js)) + js + packet["depth"].tobytes()


class Client:
    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self.q: asyncio.Queue = asyncio.Queue()
        self.frame_pending = False
        self.audio_fn = None
        self.rgb = False
        self.rgb_pending = False
        self.session = None
        self.id = uuid.uuid4().hex


class ConsoleServer:
    def __init__(self, app: EchoSense, host: str, port: int, *, auth_path=None):
        self.es = app
        self.host, self.port = host, port
        settings_path = getattr(getattr(app, "settings", None), "path", Path("~/.echosense/settings.json").expanduser())
        self.auth = AuthStore(auth_path or os.environ.get("ECHOSENSE_AUTH_DB") or
                              Path(settings_path).parent / "accounts.sqlite3")
        self.supervision = SupervisedTraining(app, self.auth)
        self._auth_hash_lock = asyncio.Lock()
        self.public_origin = os.environ.get("ECHOSENSE_ORIGIN", "").rstrip("/")
        if self.public_origin:
            origin = urlsplit(self.public_origin)
            if (origin.scheme not in ("http", "https") or not origin.hostname or origin.path or
                    origin.query or origin.fragment or origin.username or origin.password):
                raise ValueError("ECHOSENSE_ORIGIN must be an http(s) origin without a path.")
        self.clients: set[Client] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._frame_bytes: bytes | None = None
        self._rgb_bytes: bytes | None = None
        self.web = web.Application(client_max_size=8192,
                                   middlewares=[self._security_headers, self._authenticate, self._revalidate])
        self.web.router.add_get("/login", self.login_page)
        self.web.router.add_get("/api/auth/status", self.auth_status)
        self.web.router.add_post("/api/auth/setup", self.auth_setup)
        self.web.router.add_post("/api/auth/login", self.auth_login)
        self.web.router.add_post("/api/auth/logout", self.auth_logout)
        self.web.router.add_post("/api/auth/password", self.auth_password)
        self.web.router.add_post("/api/auth/accept-invite", self.auth_accept_invite)
        self.web.router.add_get("/api/caregivers", self.caregivers)
        self.web.router.add_post("/api/caregivers/invite", self.caregiver_invite)
        self.web.router.add_post("/api/caregivers/{user_id}/access", self.caregiver_access)
        self.web.router.add_get("/api/training/history", self.training_history)
        self.web.router.add_get("/api/fitting", self.fitting_state)
        self.web.router.add_post("/api/fitting", self.fitting_save)
        self.web.router.add_post("/api/fitting/preview", self.fitting_preview)
        self.web.router.add_post("/api/fitting/calibrate", self.fitting_calibrate)
        self.web.cleanup_ctx.append(self._supervision_lifecycle)
        self.web.router.add_get("/", self.index)
        self.web.router.add_get("/parents", self.parents)
        self.web.router.add_get("/ws", self.ws_handler)
        self.web.router.add_get("/api/state", self.api_state)
        self.web.router.add_get("/api/perf", self.api_perf)
        self.web.router.add_static("/static/", WEB_DIR)

    @staticmethod
    def _add_security_headers(response):
        if not response.prepared:
            response.headers.update({
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "same-origin",
            })

    @web.middleware
    async def _security_headers(self, request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            self._add_security_headers(exc)
            raise
        self._add_security_headers(response)
        return response

    @web.middleware
    async def _authenticate(self, request, handler):
        public = {"/login", "/api/auth/status", "/api/auth/setup", "/api/auth/login",
                  "/static/auth.js", "/static/auth.css", "/static/parent.css", "/api/auth/accept-invite"}
        # Validate websocket origins as well as HTTP mutations, including login/setup.
        if request.method not in ("GET", "HEAD") or request.path == "/ws":
            if request.headers.get("Origin") != (self.public_origin or f"{request.scheme}://{request.host}"):
                raise web.HTTPForbidden(text="This request must come from the EchoSense app.")
        session = self.auth.session(request.cookies.get(COOKIE, ""))
        request["session"] = session
        if request.path not in public and session is None:
            if request.path.startswith("/api/") or request.path == "/ws":
                return web.json_response({"error": "Sign in to continue."}, status=401)
            raise web.HTTPFound("/login")
        if session and request.method not in ("GET", "HEAD") and request.path not in (
                "/api/auth/login", "/api/auth/setup", "/api/auth/accept-invite"):
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode()):
                raise web.HTTPForbidden(text="Your session changed. Reload the page and try again.")
        return await handler(request)

    async def login_page(self, request):
        if request["session"]:
            raise web.HTTPFound("/parents")
        return web.Response(text=(WEB_DIR / "login.html").read_text(), content_type="text/html")

    async def auth_status(self, request):
        session = request["session"]
        return web.json_response({"setup_required": not self.auth.has_account(),
                                  "authenticated": session is not None,
                                  "username": session["username"] if session else None,
                                  "csrf": session["csrf"] if session else None,
                                  "user_id": session["user_id"] if session else None,
                                  "role": session["role"] if session else None,
                                  "permissions": session["permissions"] if session else [],
                                  "expires": session["expires"] if session else None})

    async def _auth_body(self, request):
        if request.content_type != "application/json":
            raise web.HTTPBadRequest(text="Send account details as JSON.")
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="Invalid account details.") from None
        if not isinstance(body, dict) or any(not isinstance(v, str) for v in body.values()):
            raise web.HTTPBadRequest(text="Invalid account details.")
        if any(len(v) > 128 for v in body.values()):
            raise web.HTTPBadRequest(text="Account details are too long.")
        return body

    def _signed_in(self, request, username=None):
        previous = request["session"]
        if previous:
            self.auth.revoke(previous["digest"])
        user = self.auth.user(username) if username else None
        token, session = self.auth.create_session(user["id"] if user else 1)
        response = web.json_response({"ok": True})
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=request.secure or self.public_origin.startswith("https://"),
                            samesite="Strict", path="/")
        return response

    async def auth_setup(self, request):
        if self.auth.has_account():
            return web.json_response({"error": "This device already has a parent account. Sign in instead."}, status=409)
        body = await self._auth_body(request)
        if not self.auth.allow_attempt(request.remote or "unknown"):
            return web.json_response({"error": "Too many attempts. Wait one minute and try again."}, status=429,
                                     headers={"Retry-After": "60"})
        try:
            async with self._auth_hash_lock:
                await asyncio.to_thread(self.auth.create_owner, body.get("username", "").strip(),
                                        body.get("password", ""), body.get("code", ""))
        except (ValueError, PermissionError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return self._signed_in(request, body.get("username", "").strip())

    async def auth_login(self, request):
        body = await self._auth_body(request)
        if not self.auth.allow_attempt(request.remote or "unknown"):
            return web.json_response({"error": "Too many attempts. Wait one minute and try again."}, status=429,
                                     headers={"Retry-After": "60"})
        async with self._auth_hash_lock:
            verified = await asyncio.to_thread(self.auth.verify, body.get("username", "").strip(),
                                               body.get("password", ""))
        if not verified:
            return web.json_response({"error": "Username or password is incorrect."}, status=401)
        return self._signed_in(request, body.get("username", "").strip())

    async def _close_sessions(self, digest=None, user_id=None):
        for client in list(self.clients):
            if ((digest is None or client.session["digest"] == digest) and
                    (user_id is None or client.session["user_id"] == user_id)):
                self.supervision.disconnected(client.id)
                await client.ws.close(code=4001, message=b"Please sign in again")

    async def auth_logout(self, request):
        self.auth.revoke(request["session"]["digest"])
        await self._close_sessions(request["session"]["digest"])
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response

    async def auth_password(self, request):
        body = await self._auth_body(request)
        if not self.auth.allow_attempt(request.remote or "unknown"):
            return web.json_response({"error": "Too many attempts. Wait one minute and try again."}, status=429)
        try:
            async with self._auth_hash_lock:
                changed = await asyncio.to_thread(self.auth.change_password, request["session"]["username"],
                                                   body.get("current", ""), body.get("password", ""))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        if not changed:
            return web.json_response({"error": "Your current password is incorrect."}, status=400)
        await self._close_sessions(user_id=request["session"]["user_id"])
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE, path="/")
        return response

    @staticmethod
    def _require(session, permission):
        if permission not in session["permissions"]:
            raise web.HTTPForbidden(text="Your caregiver account does not have this permission.")

    async def auth_accept_invite(self, request):
        body = await self._auth_body(request)
        if not self.auth.allow_attempt(request.remote or "unknown"):
            return web.json_response({"error": "Too many attempts. Wait one minute and try again."}, status=429)
        try:
            async with self._auth_hash_lock:
                await asyncio.to_thread(self.auth.accept_invitation, body.get("username", "").strip(),
                                        body.get("password", ""), body.get("code", ""))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return self._signed_in(request, body.get("username", "").strip())

    async def caregivers(self, request):
        self._require(request["session"], "accounts")
        return web.json_response({"users": self.auth.list_users(), "roles": {
            role: list(permissions) for role, permissions in ROLE_PERMISSIONS.items() if role != "owner"}})

    async def caregiver_invite(self, request):
        self._require(request["session"], "accounts")
        body = await self._auth_body(request)
        try:
            code = self.auth.invite(body.get("username", "").strip(), body.get("role", ""))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"code": code, "expires_in": 86400})

    async def caregiver_access(self, request):
        self._require(request["session"], "accounts")
        body = await self._auth_body(request)
        try:
            user_id = int(request.match_info["user_id"])
            if body.get("disabled") not in ("true", "false"):
                raise ValueError("Choose whether this caregiver is disabled.")
            self.auth.disable_user(user_id, body["disabled"] == "true")
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        await self._close_sessions(user_id=user_id)
        return web.json_response({"ok": True})

    async def training_history(self, request):
        self._require(request["session"], "view")
        return web.json_response({"sessions": self.supervision.history(), "supervision": self.supervision.snapshot()})

    async def fitting_state(self, request):
        return web.json_response({"settings": self.es.settings.to_dict(), "coverage": self.es.coverage(),
                                  "sensor": self.es.sensor_spec()})

    async def _fitting_body(self, request):
        self._require(request["session"], "configure")
        if request.content_type != "application/json":
            raise web.HTTPBadRequest(text="Send fitting measurements as JSON.")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Invalid fitting measurements.")
            head = number(body.get("head_height"), 0.4, 2.3)
            ear = number(body.get("ear_height"), 0.3, head)
            mount = number(body.get("mount_height"), 0.2, ear)
            pitch = number(body.get("mount_pitch"), -40, 20)
            return {"head_height": head, "ear_height": ear, "mount_height": mount,
                    "mount_pitch": pitch, "fitting_checked": body.get("fitting_checked") is True}
        except (ValueError, TypeError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None

    async def fitting_preview(self, request):
        values = await self._fitting_body(request)
        result = coverage(self.es.sensor_spec(), values["mount_height"], values["mount_pitch"],
                          head_height=values["head_height"],
                          corridor_half=self.es.cfg["processing"]["corridor_half_width"])
        return web.json_response({"coverage": result})

    async def fitting_save(self, request):
        values = await self._fitting_body(request)
        if (self.supervision.snapshot() or {}).get("status") == "active":
            raise web.HTTPConflict(text="Pause practice before changing fitting.")
        try:
            self.es.configure_fitting(values)
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        return await self.fitting_state(request)

    async def fitting_calibrate(self, request):
        self._require(request["session"], "configure")
        if (self.supervision.snapshot() or {}).get("status") == "active":
            raise web.HTTPConflict(text="Pause practice before calibrating.")
        calibration = await asyncio.to_thread(self.es.processor.calibrate_floor)
        if (self.supervision.snapshot() or {}).get("status") == "active":
            raise web.HTTPConflict(text="Practice started during calibration. Pause it and calibrate again.")
        if not calibration:
            raise web.HTTPConflict(text="No fitted floor is available. Face a clear floor and try again.")
        try:
            height = number(calibration["height"], 0.2, 2.3)
            pitch = number(calibration["pitch"], -40, 20)
        except ValueError:
            raise web.HTTPConflict(text="Floor calibration is outside the fitting range. Check the mount.") from None
        self.es.settings.update(mount_height=height, mount_pitch=pitch, fitting_checked=False)
        self.es._apply_mount()
        self.es.processor.reset()
        return await self.fitting_state(request)

    async def _supervision_lifecycle(self, app):
        async def watch():
            while True:
                await asyncio.sleep(0.5)
                if self.supervision.tick():
                    self._broadcast({"type": "supervision", "session": self.supervision.snapshot()})
        task = asyncio.create_task(watch())
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        for client in list(self.clients):
            self.supervision.disconnected(client.id)
            await client.ws.close()

    # ------------------------------------------------------------ http
    async def index(self, _req):
        # version-stamp the console's own files so a browser never runs a stale app.js against a
        # newer page (that is what broke "Show camera" after an update)
        html = (WEB_DIR / "index.html").read_text()
        for name in ("app.js", "style.css", "auth.js"):
            v = int((WEB_DIR / name).stat().st_mtime)
            html = html.replace(f"/static/{name}", f"/static/{name}?v={v}")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})

    async def parents(self, _req):
        """Local caregiver companion; media remains in the parent's browser."""
        html = (WEB_DIR / "parent.html").read_text()
        for name in ("parent.js", "parent.css", "auth.js", "auth.css"):
            version = (WEB_DIR / name).stat().st_mtime_ns
            html = html.replace(f"/static/{name}", f"/static/{name}?v={version}")
        return web.Response(text=html, content_type="text/html",
                            headers={"Cache-Control": "no-cache"})

    @web.middleware
    async def _revalidate(self, request, handler):
        resp = await handler(request)
        if request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"  # always check; unchanged files cost a 304
        return resp

    async def api_state(self, _req):
        es = self.es
        return web.json_response({"source": getattr(es.source, "kind", None), "health": es.health.state,
                                  "settings": es.settings.to_dict(), "metrics": es.metrics(),
                                  "battery": es.battery.percent, "cues": [{k: c.get(k) for k in ("kind", "r", "az")} for c in es.cues]})

    async def api_perf(self, _req):
        p = self.es.perf
        return web.json_response({"host": p.host, "latest": p.latest, "summary": p.summary()})

    # ------------------------------------------------------- app hooks
    def _on_frame(self, packet: dict):  # processing thread
        packet = {**packet, "state": {**packet["state"], "supervision": self.supervision.snapshot()}}
        data = encode_frame(packet)
        self.loop.call_soon_threadsafe(self._push_frame, data)

    def _push_frame(self, data: bytes):
        self._frame_bytes = data
        for c in self.clients:
            if not c.frame_pending:
                c.frame_pending = True
                c.q.put_nowait(("frame", None))

    def _on_rgb(self, data: bytes):  # processing thread
        self.loop.call_soon_threadsafe(self._push_rgb, data)

    def _push_rgb(self, data: bytes):
        self._rgb_bytes = data
        for c in self.clients:
            if c.rgb and not c.rgb_pending:
                c.rgb_pending = True
                c.q.put_nowait(("rgb", None))

    def _rgb_count(self):
        self.es.set_rgb_viewers(sum(1 for c in self.clients if c.rgb))

    def _on_event(self, ev: dict):
        self.loop.call_soon_threadsafe(self._broadcast, ev)

    def _broadcast(self, obj: dict):
        for c in self.clients:
            c.q.put_nowait(("json", obj))

    # ------------------------------------------------------- websocket
    async def ws_handler(self, request):
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        c = Client(ws)
        c.session = request["session"]
        self.clients.add(c)
        await ws.send_json({**self.es.hello(), "supervision": self.supervision.snapshot()})
        sender = asyncio.create_task(self._sender(c))
        watcher = asyncio.create_task(self._watch_session(c, request.cookies.get(COOKIE, "")))
        try:
            async for msg in ws:
                current_session = self.auth.session(request.cookies.get(COOKIE, ""))
                if not current_session:
                    await ws.close(code=4001, message=b"Please sign in again")
                    break
                c.session = current_session
                if msg.type == WSMsgType.TEXT:
                    try:
                        await self._command(c, json.loads(msg.data))
                    except Exception as e:
                        request_id = None
                        with suppress(ValueError, TypeError, AttributeError):
                            request_id = json.loads(msg.data).get("request_id")
                        await ws.send_json({"type": "event", "kind": "error", "request_id": request_id,
                                            "text": str(e) if isinstance(e, (ValueError, PermissionError, web.HTTPException))
                                            else "The device could not complete this command."})
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            if self.supervision.disconnected(c.id):
                self._broadcast({"type": "supervision", "session": self.supervision.snapshot()})
            self.clients.discard(c)
            self._stop_audio(c)
            if c.rgb:
                c.rgb = False
                self._rgb_count()
            sender.cancel()
            watcher.cancel()
        return ws

    async def _watch_session(self, c: Client, token: str):
        while not c.ws.closed:
            await asyncio.sleep(min(5, max(0.01, c.session["expires"] - time.time())))
            if not self.auth.session(token):
                await c.ws.close(code=4001, message=b"Please sign in again")
                return

    async def _sender(self, c: Client):
        try:
            while True:
                kind, payload = await c.q.get()
                if c.ws.closed:
                    return
                if time.time() >= c.session["expires"]:
                    await c.ws.close(code=4001, message=b"Please sign in again")
                    return
                if kind == "frame":
                    c.frame_pending = False
                    if self._frame_bytes:
                        await c.ws.send_bytes(self._frame_bytes)
                elif kind == "rgb":
                    c.rgb_pending = False
                    if self._rgb_bytes and c.rgb:
                        await c.ws.send_bytes(self._rgb_bytes)
                elif kind == "audio":
                    await c.ws.send_bytes(b"\x02\x00\x00\x00" + payload)
                else:
                    await c.ws.send_json(payload)
        except (ConnectionResetError, asyncio.CancelledError):
            return

    def _stop_audio(self, c: Client):
        if c.audio_fn and c.audio_fn in self.es.browser_tap.listeners:
            self.es.browser_tap.listeners.remove(c.audio_fn)
        c.audio_fn = None

    async def _command(self, c: Client, m: dict):
        if not isinstance(m, dict):
            raise ValueError("Invalid device command.")
        es, t = self.es, m.get("t")
        permission = "train" if t == "training" or (t == "action" and m.get("action") == "training") else (
            "view" if t in ("hello", "rgb", "audio", "viewer") else "configure")
        self._require(c.session, permission)
        if t in ("setting", "sim", "source") and (self.supervision.snapshot() or {}).get("status") == "active":
            raise ValueError("Pause supervised practice before changing device settings.")
        loop = asyncio.get_running_loop()
        if t == "action" and m.get("action") == "training":
            status = (self.supervision.snapshot() or {}).get("status")
            cmd = "pause" if status == "active" else "resume" if status == "paused" else "start"
            result = self.supervision.handle(c.id, c.session, {"cmd": cmd})
            await c.ws.send_json({"type": "training_ack", "cmd": cmd, "session": result})
        elif t == "action":
            await loop.run_in_executor(None, es.action, m["action"], "console")
        elif t == "sim":
            if m["key"] == "sensor_preset":
                await loop.run_in_executor(None, es.sim_command, m["key"], m.get("value"))
            else:
                es.sim_command(m["key"], m.get("value"))
            if m["key"] in ("scene", "sensor_preset"):
                self._broadcast(es.hello())
        elif t == "setting":
            es.set_setting(m["key"], m.get("value"))
        elif t == "source":
            await loop.run_in_executor(None, es.switch_source, m["value"])
            self._broadcast(es.hello())
        elif t == "training":
            result = self.supervision.handle(c.id, c.session, m)
            if m.get("cmd") != "heartbeat":
                await c.ws.send_json({"type": "training_ack", "request_id": m.get("request_id"),
                                      "cmd": m.get("cmd"), "session": result})
                self._broadcast({"type": "supervision", "session": self.supervision.snapshot()})
        elif t == "map":
            es.map_command(m.get("cmd"))
        elif t == "audio":
            self._stop_audio(c)
            if m.get("on"):
                def fn(pcm, c=c):
                    def put():
                        if c.q.qsize() < 40:
                            c.q.put_nowait(("audio", pcm))
                    self.loop.call_soon_threadsafe(put)
                c.audio_fn = fn
                es.browser_tap.listeners.append(fn)
        elif t == "viewer":
            st = int(m.get("stride") or 0)
            self.es.cfg["web"]["viewer_stride"] = st if st > 0 else self.es.cfg["web"].get("viewer_stride_default", 2)
        elif t == "rgb":
            c.rgb = bool(m.get("on"))
            self._rgb_count()
        elif t == "hello":
            await c.ws.send_json(es.hello())

    # ------------------------------------------------------------ run
    async def run(self):
        self.loop = asyncio.get_running_loop()
        if not self.auth.has_account():
            print(f"\n  Parent account setup: open /login and enter this device setup code:\n  {self.auth.setup_code}\n", flush=True)
        self.es.frame_listeners.append(self._on_frame)
        self.es.event_listeners.append(self._on_event)
        self.es.rgb_listeners.append(self._on_rgb)
        self.es.perf.listeners.append(lambda sample: self.loop.call_soon_threadsafe(self._broadcast, {"type": "perf", **sample}))
        # Browsers send every localhost cookie to every port, so another local app's login
        # cookie (e.g. a Supabase auth token) can exceed aiohttp's 8 KB header limit and get
        # the console rejected. The session cookie is validated separately.
        runner = web.AppRunner(self.web, access_log=None,
                               max_line_size=65536, max_field_size=65536)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        return runner
