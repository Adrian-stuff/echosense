"""Common interface for every depth source (simulator, Kinect v2, Kinect v1, replay)."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import numpy as np


class SourceError(RuntimeError):
    """The source could not be opened (no device, missing driver ...)."""


@dataclass
class DepthFrame:
    depth: np.ndarray  # (H, W) float32 metres, 0 = invalid
    t_capture: float  # time.monotonic() when the frame was captured / received
    seq: int
    intrinsics: dict  # width, height, fx, fy, cx, cy
    source: str
    extra: dict = field(default_factory=dict)


class DepthSource:
    kind = "base"
    label = "Depth source"

    def __init__(self):
        self._lock = threading.Condition()
        self._latest: DepthFrame | None = None
        self._seq = 0
        self.running = False
        self.error: str | None = None
        self.intrinsics: dict = {}

    # subclasses push frames from their own thread
    def _publish(self, depth: np.ndarray, t_capture: float | None = None, **extra):
        with self._lock:
            self._seq += 1
            self._latest = DepthFrame(depth, t_capture or time.monotonic(), self._seq, self.intrinsics, self.kind, extra)
            self._lock.notify_all()

    def read(self, timeout: float = 0.5, after_seq: int = 0) -> DepthFrame | None:
        """Return the newest frame with seq > after_seq (older frames are dropped)."""
        deadline = time.monotonic() + timeout
        with self._lock:
            while self._latest is None or self._latest.seq <= after_seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.running:
                    return None
                self._lock.wait(remaining)
            return self._latest

    def start(self):
        self.running = True

    def stop(self):
        self.running = False
        with self._lock:
            self._lock.notify_all()

    def spec(self, cfg: dict) -> dict:
        """What this sensor sees: intrinsics, depth range, model. Drives the viewer and coverage figures."""
        s = cfg["sensor"]
        out = {"model": self.kind, "label": self.label, "min_depth": s["min_depth"], "max_depth": s["max_depth"],
               "fps": s["fps"]}
        out.update(self.intrinsics)
        return out

    def info(self) -> dict:
        return {"kind": self.kind, "label": self.label, "intrinsics": self.intrinsics, "error": self.error}
