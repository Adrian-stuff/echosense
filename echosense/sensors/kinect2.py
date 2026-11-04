"""Real Microsoft Kinect v2 through libfreenect2.

libfreenect2 is a C++ library, so instead of depending on fragile Python
bindings we run a tiny native helper, ``echosense-kinect2`` (see
``native/kinect2_bridge``), and read frames from its stdout:

    line 1   : JSON  {"type": "hello", "serial": ..., "fx": ..., ...}
               or    {"type": "error", "message": ...}   (then exit)
    frames   : b"ESD2" | u32 seq | f64 t | u32 width | u32 height | float32[h*w] depth (mm)

The same protocol works on macOS, the Orange Pi, and the Docker virtual device;
``echosense-kinect2 --fake`` produces synthetic frames for testing without hardware.
"""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from ..config import PROJECT_DIR
from .base import DepthSource, SourceError

HEADER = struct.Struct("<4sIdII")
MAGIC = b"ESD2"
BRIDGE_NAME = "echosense-kinect2"


def find_bridge(cfg_path: str = "") -> str | None:
    cands = [cfg_path, os.environ.get("ECHOSENSE_KINECT2_BRIDGE", "")]
    cands += [str(PROJECT_DIR / "native" / "build" / BRIDGE_NAME), shutil.which(BRIDGE_NAME) or ""]
    for c in cands:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


class Kinect2Source(DepthSource):
    kind = "kinect2"
    label = "Kinect v2 (libfreenect2)"

    def __init__(self, cfg: dict, fake: bool = False, open_timeout: float = 8.0):
        super().__init__()
        self.cfg = cfg
        s = cfg["sensor"]
        self.flip = bool(s.get("flip_horizontal", True))
        self.fake = fake
        self.open_timeout = open_timeout
        self.bridge = find_bridge(s.get("kinect2_bridge", ""))
        self.proc: subprocess.Popen | None = None
        self.hello: dict = {}
        self._thread = None
        self._stderr_tail: list[str] = []
        if fake:
            self.label = "Kinect v2 bridge (fake frames)"

    def start(self):
        if not self.bridge:
            raise SourceError("echosense-kinect2 bridge not built. Run scripts/setup_kinect2_macos.sh "
                              "or deploy/install_orangepi.sh (see README).")
        s = self.cfg["sensor"]
        cmd = [self.bridge, "--min-depth", str(s["min_depth"]), "--max-depth", str(s["max_depth"]),
               "--pipeline", s.get("kinect2_pipeline", "auto")]
        if self.fake:
            cmd.append("--fake")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        line = self._readline_with_timeout(self.open_timeout)
        if not line:
            self._kill()
            raise SourceError("Kinect v2 bridge did not answer. " + " ".join(self._stderr_tail[-3:]))
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            self._kill()
            raise SourceError(f"Kinect v2 bridge sent garbage: {line[:80]!r}")
        if msg.get("type") != "hello":
            self._kill()
            raise SourceError(msg.get("message", "Kinect v2 not available"))
        self.hello = msg
        self.intrinsics = {k: msg[k] for k in ("width", "height", "fx", "fy", "cx", "cy")}
        if self.flip:  # mirror principal point together with the image
            self.intrinsics["cx"] = self.intrinsics["width"] - 1 - self.intrinsics["cx"]
        super().start()
        self._thread = threading.Thread(target=self._reader, name="kinect2-reader", daemon=True)
        self._thread.start()

    def _readline_with_timeout(self, timeout):
        out = {}

        def rd():
            out["line"] = self.proc.stdout.readline().decode("utf-8", "replace").strip()

        th = threading.Thread(target=rd, daemon=True)
        th.start()
        th.join(timeout)
        return out.get("line")

    def _drain_stderr(self):
        for raw in iter(self.proc.stderr.readline, b""):
            self._stderr_tail.append(raw.decode("utf-8", "replace").strip())
            del self._stderr_tail[:-20]

    def _read_exact(self, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            chunk = self.proc.stdout.read(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)

    def _reader(self):
        while self.running:
            hdr = self._read_exact(HEADER.size)
            if hdr is None:
                break
            magic, seq, t_dev, w, h = HEADER.unpack(hdr)
            if magic != MAGIC:
                self.error = "bridge stream out of sync"
                break
            payload = self._read_exact(w * h * 4)
            if payload is None:
                break
            t = time.monotonic()
            depth = np.frombuffer(payload, np.float32).reshape(h, w) * np.float32(0.001)
            if self.flip:
                depth = depth[:, ::-1]
            self._publish(np.ascontiguousarray(depth), t, device_seq=seq)
        if self.running:
            self.error = self.error or ("Kinect v2 disconnected. " + " ".join(self._stderr_tail[-2:]))
            self.running = False
            with self._lock:
                self._lock.notify_all()

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def stop(self):
        super().stop()
        self._kill()

    def spec(self, cfg: dict) -> dict:
        d = super().spec(cfg)
        d.update(model="kinect_v2", min_depth=0.5, max_depth=4.5)
        return d

    def info(self):
        d = super().info()
        d.update(serial=self.hello.get("serial"), pipeline=self.hello.get("pipeline"), flip=self.flip, bridge=self.bridge)
        return d
