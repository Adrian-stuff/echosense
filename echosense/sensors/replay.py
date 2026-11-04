"""Record depth streams to disk and play them back.

Recording field sessions with the real Kinect lets the team re-run the exact
same depth data through new versions of the pipeline on a laptop or in the
virtual Orange Pi.

File format (``.esd``)::

    b"ESDREC1\\n"
    JSON header line  {"intrinsics": {...}, "source": "kinect2", "created": "..."}
    repeated: <d t_rel> <I nbytes> zlib(uint16 depth in millimetres, H x W)
"""
from __future__ import annotations

import json
import struct
import threading
import time
import zlib
from datetime import datetime

import numpy as np

from .base import DepthFrame, DepthSource, SourceError

MAGIC = b"ESDREC1\n"
REC = struct.Struct("<dI")


class Recorder:
    def __init__(self, path: str, intrinsics: dict, source: str):
        self.f = open(path, "wb")
        self.f.write(MAGIC)
        hdr = {"intrinsics": intrinsics, "source": source, "created": datetime.now().isoformat(timespec="seconds")}
        self.f.write((json.dumps(hdr) + "\n").encode())
        self.t0 = None
        self.frames = 0
        self.path = path

    def write(self, frame: DepthFrame):
        if self.t0 is None:
            self.t0 = frame.t_capture
        mm = np.clip(frame.depth * 1000.0, 0, 65535).astype("<u2")
        data = zlib.compress(mm.tobytes(), 1)
        self.f.write(REC.pack(frame.t_capture - self.t0, len(data)))
        self.f.write(data)
        self.frames += 1

    def close(self):
        self.f.close()


def read_recording(path: str):
    with open(path, "rb") as f:
        if f.read(len(MAGIC)) != MAGIC:
            raise SourceError(f"{path} is not an Echo-Sense depth recording")
        hdr = json.loads(f.readline())
        intr = hdr["intrinsics"]
        h, w = int(intr["height"]), int(intr["width"])
        while True:
            rec = f.read(REC.size)
            if len(rec) < REC.size:
                break
            t, n = REC.unpack(rec)
            mm = np.frombuffer(zlib.decompress(f.read(n)), "<u2").reshape(h, w)
            yield hdr, t, mm.astype(np.float32) * np.float32(0.001)


class ReplaySource(DepthSource):
    kind = "replay"
    label = "Recorded depth (replay)"

    def __init__(self, path: str, loop: bool = True):
        super().__init__()
        self.path, self.loop = path, loop
        first = next(read_recording(path), None)
        if first is None:
            raise SourceError(f"{path} contains no frames")
        self.header = first[0]
        self.intrinsics = dict(self.header["intrinsics"])
        self.label = f"Replay: {path}"
        self._thread = None

    def start(self):
        super().start()
        self._thread = threading.Thread(target=self._run, name="replay", daemon=True)
        self._thread.start()

    def _run(self):
        while self.running:
            start = time.monotonic()
            for _hdr, t, depth in read_recording(self.path):
                if not self.running:
                    return
                delay = start + t - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self._publish(depth, time.monotonic())
            if not self.loop:
                break
        self.running = False
