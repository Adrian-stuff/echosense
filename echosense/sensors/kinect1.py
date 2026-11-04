"""Original Kinect (Xbox 360, "Kinect v1") through libfreenect's Python wrapper.

Optional: only used if the ``freenect`` module is importable. Handy for bench
work when a Kinect v1 is the sensor at hand; the proposal's device uses v2.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from .base import DepthSource, SourceError


class Kinect1Source(DepthSource):
    kind = "kinect1"
    label = "Kinect v1 (libfreenect)"

    def __init__(self, cfg: dict):
        super().__init__()
        try:
            import freenect  # type: ignore
        except ImportError as e:
            raise SourceError("Python 'freenect' module not installed (libfreenect wrappers/python).") from e
        self.fn = freenect
        # approximate factory intrinsics of the Kinect v1 depth camera (640x480)
        self.intrinsics = {"width": 640, "height": 480, "fx": 585.0, "fy": 585.0, "cx": 319.5, "cy": 239.5}
        self._thread = None

    def start(self):
        res = self.fn.sync_get_depth(format=self.fn.DEPTH_MM)
        if res is None:
            raise SourceError("No Kinect v1 found.")
        super().start()
        self._thread = threading.Thread(target=self._run, name="kinect1", daemon=True)
        self._thread.start()

    def _run(self):
        while self.running:
            res = self.fn.sync_get_depth(format=self.fn.DEPTH_MM)
            if res is None:
                self.error = "Kinect v1 disconnected"
                self.running = False
                break
            depth_mm, _ts = res
            self._publish(depth_mm.astype(np.float32) * 0.001, time.monotonic())

    def stop(self):
        super().stop()
        try:
            self.fn.sync_stop()
        except Exception:
            pass
