"""ctypes binding for Kinect v1 depth, colour and optional accelerometer.

Model 1473 routes motor/accelerometer controls through the audio device. libfreenect
only detects that route while opening the camera, so request CAMERA | MOTOR together
when audios.bin is installed. The driver uploads firmware as needed; no audio is streamed.
If that fails, retry camera-only so depth remains available. Model 1414 also supports a
separate motor-only context. Accelerometer reads never move the tilt motor.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import threading
from pathlib import Path

import numpy as np

DEVICE_MOTOR, DEVICE_CAMERA, DEVICE_AUDIO = 1, 2, 4
RES_MEDIUM = 1
DEPTH_MM = 5  # FREENECT_DEPTH_MM: 640x480 uint16 millimetres, 0 = no reading
VIDEO_RGB = 0  # FREENECT_VIDEO_RGB: 640x480x3 uint8 (debayered by libfreenect)


class FrameMode(ctypes.Structure):
    _fields_ = [("reserved", ctypes.c_uint32), ("resolution", ctypes.c_int), ("format", ctypes.c_int32),
                ("bytes", ctypes.c_int32), ("width", ctypes.c_int16), ("height", ctypes.c_int16),
                ("data_bits_per_pixel", ctypes.c_int8), ("padding_bits_per_pixel", ctypes.c_int8),
                ("framerate", ctypes.c_int8), ("is_valid", ctypes.c_int8)]


class RawTiltState(ctypes.Structure):
    _fields_ = [("accelerometer_x", ctypes.c_int16), ("accelerometer_y", ctypes.c_int16),
                ("accelerometer_z", ctypes.c_int16), ("tilt_angle", ctypes.c_int8), ("tilt_status", ctypes.c_int)]


DEPTH_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32)
VIDEO_CB = DEPTH_CB


PID_MOTOR_1414, PID_AUDIO_1473, PID_CAMERA_1414, PID_CAMERA_1473 = 0x02B0, 0x02AD, 0x02AE, 0x02BF


def kinect_usb_products() -> set[int] | None:
    """USB product ids of the Microsoft (0x045E) devices attached, read from the OS without opening
    anything (Linux sysfs, macOS ioreg). None if it can't be told on this system."""
    import glob
    import re
    import subprocess
    import sys
    try:
        if sys.platform.startswith("linux"):
            out = set()
            for d in glob.glob("/sys/bus/usb/devices/*/"):
                try:
                    if open(d + "idVendor").read().strip().lower() == "045e":
                        out.add(int(open(d + "idProduct").read().strip(), 16))
                except OSError:
                    continue
            return out
        if sys.platform == "darwin":
            txt = subprocess.run(["ioreg", "-p", "IOUSB", "-l", "-w0"], capture_output=True, text=True, timeout=3).stdout
            out = {int(m) for m in re.findall(r'"idProduct" = (\d+)', txt)}
            return {p for p in out if p in (PID_MOTOR_1414, PID_AUDIO_1473, PID_CAMERA_1414, PID_CAMERA_1473)}
    except Exception:
        return None
    return None


def find_audio_firmware() -> Path | None:
    """Match libfreenect's firmware search locations; never download during startup."""
    folders = [os.environ.get("LIBFREENECT_FIRMWARE_PATH", ""), str(Path.cwd()),
               str(Path.home() / ".libfreenect"), "/usr/local/share/libfreenect",
               "/usr/share/libfreenect", str(Path.cwd().parent / "Resources")]
    for folder in folders:
        path = Path(folder) / "audios.bin"
        if folder and path.is_file() and path.stat().st_size > 0:
            return path
    return None


def find_library() -> str | None:
    # The optional macOS build avoids resetting model 1473's audio/control device.
    import sys
    local = str(Path.home() / ".local/share/echosense/libfreenect/libfreenect.dylib") if sys.platform == "darwin" else ""
    cands = [os.environ.get("ECHOSENSE_LIBFREENECT", ""), local, ctypes.util.find_library("freenect") or "",
             "/opt/homebrew/lib/libfreenect.dylib", "/usr/local/lib/libfreenect.dylib",
             "/usr/lib/aarch64-linux-gnu/libfreenect.so.0.5", "/usr/lib/arm-linux-gnueabihf/libfreenect.so.0.5",
             "/usr/lib/x86_64-linux-gnu/libfreenect.so.0.5", "/usr/local/lib/libfreenect.so"]
    for c in cands:
        if c and (Path(c).exists() or "/" not in c):
            try:
                ctypes.CDLL(c)
                return c
            except OSError:
                continue
    return None


class FreenectDepth:
    """Streams Kinect v1 depth (mm) to ``on_frame(np.ndarray[480, 640] uint16, timestamp)`` and,
    while ``want_video`` is set, colour to ``on_video(np.ndarray[480, 640, 3] uint8, timestamp)``."""

    def __init__(self, on_frame, index: int = 0, libpath: str | None = None, on_video=None, accelerometer=False):
        path = libpath or find_library()
        if not path:
            raise OSError("libfreenect not found (macOS: brew install libfreenect; Debian: apt install libfreenect0.5)")
        self.accelerometer = bool(accelerometer)
        self.accel_error = None
        self.lib = lib = ctypes.CDLL(path)
        lib.freenect_init.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        lib.freenect_select_subdevices.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.freenect_select_subdevices.restype = None
        lib.freenect_num_devices.argtypes = [ctypes.c_void_p]
        lib.freenect_open_device.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
        lib.freenect_find_depth_mode.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.freenect_find_depth_mode.restype = FrameMode
        lib.freenect_set_depth_mode.argtypes = [ctypes.c_void_p, FrameMode]
        lib.freenect_set_depth_callback.argtypes = [ctypes.c_void_p, DEPTH_CB]
        lib.freenect_set_depth_callback.restype = None
        lib.freenect_find_video_mode.argtypes = [ctypes.c_int, ctypes.c_int]
        lib.freenect_find_video_mode.restype = FrameMode
        lib.freenect_set_video_mode.argtypes = [ctypes.c_void_p, FrameMode]
        lib.freenect_set_video_callback.argtypes = [ctypes.c_void_p, VIDEO_CB]
        lib.freenect_set_video_callback.restype = None
        for fn in ("freenect_start_depth", "freenect_stop_depth", "freenect_close_device",
                   "freenect_start_video", "freenect_stop_video"):
            getattr(lib, fn).argtypes = [ctypes.c_void_p]
        lib.freenect_process_events.argtypes = [ctypes.c_void_p]
        lib.freenect_shutdown.argtypes = [ctypes.c_void_p]
        lib.freenect_set_flag.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.freenect_set_flag.restype = ctypes.c_int
        lib.freenect_set_log_level.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.freenect_set_log_level.restype = None
        self.on_frame = on_frame
        self.index = index
        self.ctx = ctypes.c_void_p()
        self.dev = ctypes.c_void_p()
        self._cb = DEPTH_CB(self._depth_cb)  # keep a reference: ctypes callbacks must outlive their use
        self._vcb = VIDEO_CB(self._video_cb)
        self.on_video = on_video
        self.want_video = False  # applied by the event thread (libfreenect calls stay on one thread)
        self.want_near = None  # None = leave as is, True/False = request hardware near mode
        self.near_result = None  # True if the Kinect accepted near mode, False if it refused
        self.video_on = False
        self.vmode = None
        self._run = False
        self._thread = None
        self.mode = None

    def open(self):
        lib = self.lib
        if lib.freenect_init(ctypes.byref(self.ctx), None) < 0:
            raise OSError("freenect_init failed")
        lib.freenect_set_log_level(self.ctx, 1)  # errors only
        lib.freenect_select_subdevices(self.ctx, DEVICE_CAMERA | (DEVICE_MOTOR if self.accelerometer else 0))
        n = lib.freenect_num_devices(self.ctx)
        if n <= self.index:
            lib.freenect_shutdown(self.ctx)
            raise OSError("No Kinect v1 found (check USB and the 12 V power adapter)")
        if lib.freenect_open_device(self.ctx, ctypes.byref(self.dev), self.index) < 0:
            self.close()
            if self.accelerometer:
                # Firmware/USB permissions must not prevent depth from working.
                self.accelerometer = False
                self.accel_error = "camera + accelerometer open failed; check audios.bin and audio USB connection"
                return self.open()
            raise OSError("Kinect v1 found but could not be opened (is another program using it?)")
        self.mode = lib.freenect_find_depth_mode(RES_MEDIUM, DEPTH_MM)
        if not self.mode.is_valid or lib.freenect_set_depth_mode(self.dev, self.mode) < 0:
            self.close()
            raise OSError("Kinect v1 does not accept the millimetre depth mode")
        lib.freenect_set_depth_callback(self.dev, self._cb)
        self.vmode = lib.freenect_find_video_mode(RES_MEDIUM, VIDEO_RGB)
        if self.vmode.is_valid and lib.freenect_set_video_mode(self.dev, self.vmode) >= 0:
            lib.freenect_set_video_callback(self.dev, self._vcb)
        else:
            self.vmode = None
        if lib.freenect_start_depth(self.dev) < 0:
            self.close()
            raise OSError("Kinect v1 depth stream did not start")
        self._run = True
        self._thread = threading.Thread(target=self._events, name="freenect-events", daemon=True)
        self._thread.start()

    def _depth_cb(self, _dev, buf, timestamp):
        w, h = self.mode.width, self.mode.height
        arr = np.ctypeslib.as_array(ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint16)), shape=(h, w)).copy()
        self.on_frame(arr, timestamp)

    def _video_cb(self, _dev, buf, timestamp):
        if self.on_video is None:
            return
        w, h = self.vmode.width, self.vmode.height
        arr = np.ctypeslib.as_array(ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint8)), shape=(h, w, 3)).copy()
        self.on_video(arr, timestamp)

    def _events(self):
        while self._run:
            if self.want_near is not None:
                # FREENECT_NEAR_MODE (1 << 18): only Kinect for Windows hardware accepts it
                self.near_result = self.lib.freenect_set_flag(self.dev, 1 << 18, 1 if self.want_near else 0) >= 0
                self.want_near = None
            if self.vmode is not None and self.want_video != self.video_on:
                if self.want_video:
                    self.video_on = self.lib.freenect_start_video(self.dev) >= 0
                else:
                    self.lib.freenect_stop_video(self.dev)
                    self.video_on = False
            if self.lib.freenect_process_events(self.ctx) < 0:
                self._run = False
                self.on_frame(None, 0)  # tell the owner the device went away
                break

    def close(self):
        was = self._run
        self._run = False
        if self.dev:
            if was:
                if self.video_on:
                    self.lib.freenect_stop_video(self.dev)
                    self.video_on = False
                self.lib.freenect_stop_depth(self.dev)
            if self._thread and self._thread is not threading.current_thread():
                self._thread.join(timeout=2)
            self.lib.freenect_close_device(self.dev)
            self.dev = ctypes.c_void_p()
        if self.ctx:
            self.lib.freenect_shutdown(self.ctx)
            self.ctx = ctypes.c_void_p()


class FreenectAccel:
    """Reads the Kinect v1 accelerometer (m/s^2, libfreenect's own axes) about ``rate`` times a second.
    ``latest`` is ``(ax, ay, az, monotonic time)`` or None. ``open()`` raises OSError when unavailable."""

    def __init__(self, index: int = 0, libpath: str | None = None, rate: float = 20.0, device=None):
        path = libpath or find_library()
        if not path:
            raise OSError("libfreenect not found")
        self.lib = lib = ctypes.CDLL(path)
        lib.freenect_init.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        lib.freenect_select_subdevices.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.freenect_select_subdevices.restype = None
        lib.freenect_num_devices.argtypes = [ctypes.c_void_p]
        lib.freenect_open_device.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
        lib.freenect_close_device.argtypes = [ctypes.c_void_p]
        lib.freenect_shutdown.argtypes = [ctypes.c_void_p]
        lib.freenect_set_log_level.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.freenect_set_log_level.restype = None
        lib.freenect_update_tilt_state.argtypes = [ctypes.c_void_p]
        lib.freenect_update_tilt_state.restype = ctypes.c_int
        lib.freenect_get_tilt_state.argtypes = [ctypes.c_void_p]
        lib.freenect_get_tilt_state.restype = ctypes.POINTER(RawTiltState)
        lib.freenect_get_mks_accel.argtypes = [ctypes.POINTER(RawTiltState), ctypes.POINTER(ctypes.c_double),
                                               ctypes.POINTER(ctypes.c_double), ctypes.POINTER(ctypes.c_double)]
        lib.freenect_get_mks_accel.restype = None
        self.index, self.period = index, 1.0 / rate
        self._owns_device = device is None
        self.ctx = ctypes.c_void_p()
        self.dev = device.dev if device is not None else ctypes.c_void_p()
        self.latest = None
        self.failures = 0
        self._run = False
        self._thread = None

    def open(self):
        if self._owns_device:
            self._open_device()
        if self._read() is None:
            self.close()
            raise OSError("Kinect accelerometer gave no reading")
        self._run = True
        self._thread = threading.Thread(target=self._loop, name="freenect-accel", daemon=True)
        self._thread.start()

    def _open_device(self):
        lib = self.lib
        if lib.freenect_init(ctypes.byref(self.ctx), None) < 0:
            raise OSError("freenect_init failed")
        lib.freenect_set_log_level(self.ctx, 0)  # fatal only: a missing motor is not an error for us
        lib.freenect_select_subdevices(self.ctx, DEVICE_MOTOR)
        found = lib.freenect_num_devices(self.ctx) > self.index
        if not found or lib.freenect_open_device(self.ctx, ctypes.byref(self.dev), self.index) < 0:
            lib.freenect_shutdown(self.ctx)
            self.ctx = ctypes.c_void_p()
            raise OSError("no Kinect motor/accelerometer device found" if not found else
                          "Kinect accelerometer could not be opened (model 1473 needs its audio firmware for it)")

    def _read(self):
        lib = self.lib
        if lib.freenect_update_tilt_state(self.dev) < 0:
            return None
        st = lib.freenect_get_tilt_state(self.dev)
        if not st:
            return None
        x, y, z = ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
        lib.freenect_get_mks_accel(st, ctypes.byref(x), ctypes.byref(y), ctypes.byref(z))
        g = (x.value * x.value + y.value * y.value + z.value * z.value) ** 0.5
        if not 4.0 < g < 16.0:  # not a gravity reading (unplugged, garbage)
            return None
        import time
        self.latest = (x.value, y.value, z.value, time.monotonic())
        return self.latest

    def _loop(self):
        import time
        while self._run:
            t0 = time.monotonic()
            if self._read() is None:
                self.failures += 1
                if self.failures > 20:  # gone (unplugged): stop quietly, depth handles reconnects
                    self._run = False
                    break
            else:
                self.failures = 0
            time.sleep(max(0.0, self.period - (time.monotonic() - t0)))

    def close(self):
        self._run = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1)
        if self._owns_device and self.dev:
            self.lib.freenect_close_device(self.dev)
        self.dev = ctypes.c_void_p()
        if self.ctx:
            self.lib.freenect_shutdown(self.ctx)
            self.ctx = ctypes.c_void_p()
