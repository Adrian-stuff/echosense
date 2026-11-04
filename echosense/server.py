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
import struct

from aiohttp import WSMsgType, web

from .app import EchoSense
from .config import WEB_DIR


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


class ConsoleServer:
    def __init__(self, app: EchoSense, host: str, port: int):
        self.es = app
        self.host, self.port = host, port
        self.clients: set[Client] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self._frame_bytes: bytes | None = None
        self.web = web.Application()
        self.web.router.add_get("/", self.index)
        self.web.router.add_get("/ws", self.ws_handler)
        self.web.router.add_get("/api/state", self.api_state)
        self.web.router.add_get("/api/perf", self.api_perf)
        self.web.router.add_static("/static/", WEB_DIR)

    # ------------------------------------------------------------ http
    async def index(self, _req):
        return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

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
        data = encode_frame(packet)
        self.loop.call_soon_threadsafe(self._push_frame, data)

    def _push_frame(self, data: bytes):
        self._frame_bytes = data
        for c in self.clients:
            if not c.frame_pending:
                c.frame_pending = True
                c.q.put_nowait(("frame", None))

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
        self.clients.add(c)
        await ws.send_json(self.es.hello())
        sender = asyncio.create_task(self._sender(c))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        await self._command(c, json.loads(msg.data))
                    except Exception as e:
                        await ws.send_json({"type": "event", "kind": "error", "text": f"command failed: {e!r}"})
                elif msg.type == WSMsgType.ERROR:
                    break
        finally:
            self.clients.discard(c)
            self._stop_audio(c)
            sender.cancel()
        return ws

    async def _sender(self, c: Client):
        try:
            while True:
                kind, payload = await c.q.get()
                if c.ws.closed:
                    return
                if kind == "frame":
                    c.frame_pending = False
                    if self._frame_bytes:
                        await c.ws.send_bytes(self._frame_bytes)
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
        es, t = self.es, m.get("t")
        loop = asyncio.get_running_loop()
        if t == "action":
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
            es.training_command(m)
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
        elif t == "hello":
            await c.ws.send_json(es.hello())

    # ------------------------------------------------------------ run
    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.es.frame_listeners.append(self._on_frame)
        self.es.event_listeners.append(self._on_event)
        self.es.perf.listeners.append(lambda sample: self.loop.call_soon_threadsafe(self._broadcast, {"type": "perf", **sample}))
        runner = web.AppRunner(self.web, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        return runner
