"""Original Kinect (Xbox 360, "Kinect v1") through libfreenect.

Uses a small ctypes binding to libfreenect (``freenect_ctypes.py``). Depth works without
firmware; optional model 1473 accelerometer access opens camera and motor controls together
when audios.bin is installed. Needs ``brew install libfreenect`` (macOS) or
``apt install libfreenect0.5`` (Debian / Armbian). Falls back to libfreenect's own Python
module for depth if the library can't be loaded directly.

The Kinect v1 runs on USB 2.0 and computes depth inside the sensor, which is what makes it
usable on small boards such as the Orange Pi One.
"""
from __future__ import annotations

import threading
import time

import numpy as np

from .base import DepthSource, SourceError
from .freenect_ctypes import (
    PID_AUDIO_1473, PID_MOTOR_1414, FreenectAccel, FreenectDepth,
    find_audio_firmware, find_library, kinect_usb_products,
)


def kinect1_available() -> tuple[bool, str]:
    if find_library():
        return True, ""
    try:
        import freenect  # noqa: F401  type: ignore
        return True, ""
    except ImportError:
        return False, "libfreenect not installed"


class Kinect1Source(DepthSource):
    kind = "kinect1"
    label = "Kinect v1 (libfreenect)"

    def __init__(self, cfg: dict):
        super().__init__()
        s = cfg["sensor"]
        self.period = 1.0 / float(s.get("fps", 30))  # publish at most this often (lite boards: 15)
        self.flip = bool(s.get("kinect1_flip", False))
        # approximate factory intrinsics of the Kinect v1 depth camera (640x480)
        self.intrinsics = {"width": 640, "height": 480, "fx": 585.0, "fy": 585.0, "cx": 319.5, "cy": 239.5}
        if self.flip:
            self.intrinsics["cx"] = 639 - 319.5
        self._last = 0.0
        self._rgb = None
        # Kinect v1 colour camera: its own lens ~2.5 cm beside the IR camera, slightly wider view
        self.rgb_intrinsics = {"width": 640, "height": 480, "fx": 525.0, "fy": 525.0, "cx": 319.5, "cy": 239.5,
                               "baseline": float(s.get("kinect1_rgb_baseline", 0.025)), "label": "Kinect v1 colour camera"}
        self._dev = None
        self._accel = None
        self.accel_status = "off"  # off | on | unavailable: <reason>
        self.want_accel = s.get("kinect1_accelerometer", "auto")  # auto | True | False
        self._thread = None
        self._fn = None
        if not find_library():
            try:
                import freenect  # type: ignore
            except ImportError as e:
                raise SourceError("libfreenect not installed (macOS: brew install libfreenect; "
                                  "Debian/Armbian: apt install libfreenect0.5)") from e
            self._fn = freenect

    # ------------------------------------------------------------------
    def start(self):
        if self._fn is None:
            # right after another program (or a restarted Echo-Sense) lets go of the Kinect, or just
            # after it is plugged in, USB needs a moment before libfreenect can see it again
            err = None
            for attempt in range(5):
                self._dev = FreenectDepth(self._on_frame, on_video=self._on_video,
                                         accelerometer=self.want_accel is not False and
                                         (self.want_accel is True or find_audio_firmware() is not None))
                try:
                    self._dev.open()
                    err = None
                    break
                except OSError as e:
                    err = e
                    time.sleep(0.8)
            if err is not None:
                raise SourceError(str(err)) from err
            self.label = "Kinect v1 (libfreenect)"
            self._open_accel()
            super().start()
            return
        if self._fn.sync_get_depth(format=self._fn.DEPTH_MM) is None:
            raise SourceError("No Kinect v1 found.")
        super().start()
        self._thread = threading.Thread(target=self._run_sync, name="kinect1", daemon=True)
        self._thread.start()

    def _open_accel(self):
        """Read through the camera handle on 1473; only 1414 supports a motor-only context."""
        if self.want_accel is False:
            return
        device = getattr(self, "_dev", None)
        shared = device is not None and device.accelerometer
        if device is not None and device.accel_error:
            self.accel_status = "unavailable: " + device.accel_error
            return
        if not shared and self.want_accel == "auto":
            pids = kinect_usb_products()
            if pids is not None and PID_MOTOR_1414 not in pids:
                self.accel_status = ("unavailable: model 1473 needs audios.bin; run "
                                     "python scripts/setup_kinect1_firmware.py, then restart Echo-Sense"
                                     if PID_AUDIO_1473 in pids else
                                     "unavailable: no motor/audio USB device; check Kinect power and USB connection")
                return
        try:
            acc = FreenectAccel(device=device) if shared else FreenectAccel()
            acc.open()
            self._accel, self.accel_status = acc, "on"
        except OSError as e:
            self._accel, self.accel_status = None, f"unavailable: {e}"

    def accel(self):
        """Latest accelerometer reading in libfreenect's axes (m/s^2), mirrored with the image, or None."""
        a = self._accel.latest if self._accel is not None else None
        if a is None or time.monotonic() - a[3] > 0.5:
            return None
        return (-a[0] if self.flip else a[0], a[1], a[2])

    def set_near_mode(self, on: bool):
        """Ask the Kinect for hardware near mode (0.4-3 m). Kinect for Windows accepts it; the Xbox 360
        model refuses, and ``near_mode_result`` becomes False a moment later."""
        if self._dev is not None:
            self._dev.near_result = None
            self._dev.want_near = bool(on)

    @property
    def near_mode_result(self):
        return None if self._dev is None else self._dev.near_result

    @property
    def has_rgb(self):
        return self._dev is not None and self._dev.vmode is not None

    def set_rgb(self, on: bool):
        if self._dev is not None:
            self._dev.want_video = bool(on)
            if not on:
                self._rgb = None

    def _on_video(self, img, _ts):
        if self.flip:
            img = np.ascontiguousarray(img[:, ::-1])
        self._rgb = (img, time.monotonic())

    def rgb(self):
        if self._rgb is None:
            return None
        intr = dict(self.rgb_intrinsics)
        if self.flip:
            intr["cx"] = intr["width"] - 1 - intr["cx"]
            intr["baseline"] = -intr["baseline"]
        return self._rgb[0], intr, self._rgb[1]

    def _on_frame(self, mm, _ts):
        if mm is None:
            self.error = "Kinect v1 disconnected"
            self.running = False
            with self._lock:
                self._lock.notify_all()
            return
        now = time.monotonic()
        if now - self._last < self.period * 0.9:
            return  # the Kinect v1 always streams 30 fps; skip frames a lite profile won't process
        self._last = now
        depth = mm.astype(np.float32) * np.float32(0.001)
        if self.flip:
            depth = np.ascontiguousarray(depth[:, ::-1])
        acc = self.accel()
        if acc is not None:
            self._publish(depth, now, accel=acc)
        else:
            self._publish(depth, now)

    def _run_sync(self):
        while self.running:
            res = self._fn.sync_get_depth(format=self._fn.DEPTH_MM)
            if res is None:
                self._on_frame(None, 0)
                break
            self._on_frame(res[0], res[1])

    def spec(self, cfg: dict) -> dict:
        d = super().spec(cfg)
        d.update(model="kinect_v1", min_depth=0.8, max_depth=4.0)
        return d

    def stop(self):
        super().stop()
        if self._accel is not None:
            self._accel.close()
            self._accel = None
        if self._dev is not None:
            self._dev.close()
            self._dev = None
        if self._fn is not None:
            try:
                self._fn.sync_stop()
            except Exception:
                pass

    def info(self):
        d = super().info()
        d["flip"] = self.flip
        d["accelerometer"] = self.accel_status
        return d
