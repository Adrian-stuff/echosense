"""Model 1473 must request camera and motor together and share the resulting handle."""
import ctypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from echosense.sensors import freenect_ctypes as driver
from echosense.sensors import kinect1


def test_firmware_search_environment_and_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LIBFREENECT_FIRMWARE_PATH", str(tmp_path / "custom"))
    assert driver.find_audio_firmware() is None
    home_fw = tmp_path / ".libfreenect" / "audios.bin"
    home_fw.parent.mkdir()
    home_fw.write_bytes(b"firmware")
    assert driver.find_audio_firmware() == home_fw
    custom_fw = tmp_path / "custom" / "audios.bin"
    custom_fw.parent.mkdir()
    custom_fw.touch()
    assert driver.find_audio_firmware() == home_fw  # empty files cannot enable probing
    custom_fw.write_bytes(b"custom")
    assert driver.find_audio_firmware() == custom_fw


@pytest.mark.parametrize("fail_combined", [False, True])
def test_combined_open_and_camera_fallback(monkeypatch, fail_combined):
    lib = Mock()
    def init(ctx, unused):
        ctx._obj.value = 11
        return 0
    def open_device(ctx, dev, index):
        if fail_combined and lib.freenect_open_device.call_count == 1:
            return -1
        dev._obj.value = 22
        return 0
    lib.freenect_init.side_effect = init
    lib.freenect_open_device.side_effect = open_device
    lib.freenect_num_devices.return_value = 1
    lib.freenect_find_depth_mode.return_value = SimpleNamespace(is_valid=True)
    lib.freenect_find_video_mode.return_value = SimpleNamespace(is_valid=False)
    lib.freenect_set_depth_mode.return_value = 0
    lib.freenect_start_depth.return_value = 0
    monkeypatch.setattr(driver.ctypes, "CDLL", lambda path: lib)
    monkeypatch.setattr(driver.threading, "Thread", Mock())
    depth = driver.FreenectDepth(lambda *args: None, libpath="fake", accelerometer=True)
    try:
        depth.open()
        flags = [call.args[1] for call in lib.freenect_select_subdevices.call_args_list]
        assert flags == ([3, 2] if fail_combined else [3])
        assert depth.accelerometer is not fail_combined
        assert bool(depth.accel_error) is fail_combined
        assert lib.freenect_start_depth.call_count == 1
    finally:
        depth.close()
    assert lib.freenect_shutdown.call_count == (2 if fail_combined else 1)


def test_accelerometer_shares_camera_without_closing_it(monkeypatch):
    lib = Mock()
    monkeypatch.setattr(driver.ctypes, "CDLL", lambda path: lib)
    monkeypatch.setattr(driver.threading, "Thread", Mock())
    acc = driver.FreenectAccel(libpath="fake", device=SimpleNamespace(dev=ctypes.c_void_p(22)))
    monkeypatch.setattr(acc, "_read", lambda: (0, 9.8, 0, 1))
    acc.open()
    lib.freenect_init.assert_not_called()
    assert acc.dev.value == 22
    acc.close()
    lib.freenect_close_device.assert_not_called()
    lib.freenect_shutdown.assert_not_called()


@pytest.mark.parametrize("option,firmware,expected", [("auto", True, True), ("auto", False, False),
                                                     (False, True, False), (True, False, True)])
def test_source_requests_combined_handle(monkeypatch, option, firmware, expected):
    from echosense.config import load_config
    cfg = load_config()
    cfg["sensor"]["kinect1_accelerometer"] = option
    monkeypatch.setattr(kinect1, "find_library", lambda: "fake")
    monkeypatch.setattr(kinect1, "find_audio_firmware", lambda: "audios.bin" if firmware else None)
    device = Mock()
    monkeypatch.setattr(kinect1, "FreenectDepth", device)
    monkeypatch.setattr(kinect1.Kinect1Source, "_open_accel", Mock())
    src = kinect1.Kinect1Source(cfg)
    try:
        src.start()
        assert device.call_args.kwargs["accelerometer"] is expected
    finally:
        src.stop()


def test_source_uses_shared_accelerometer_handle(monkeypatch):
    src = kinect1.Kinect1Source.__new__(kinect1.Kinect1Source)
    src.want_accel = "auto"
    src._dev = SimpleNamespace(accelerometer=True, accel_error=None)
    accel_class = Mock()
    monkeypatch.setattr(kinect1, "FreenectAccel", accel_class)
    # A working combined handle must not be rejected by product-id probing.
    monkeypatch.setattr(kinect1, "kinect_usb_products", lambda: {driver.PID_AUDIO_1473})
    src._open_accel()
    accel_class.assert_called_once_with(device=src._dev)
    accel_class.return_value.open.assert_called_once()
    assert src.accel_status == "on"


def test_failed_controls_do_not_reopen_separately(monkeypatch):
    src = kinect1.Kinect1Source.__new__(kinect1.Kinect1Source)
    src.want_accel = "auto"
    src._dev = SimpleNamespace(accelerometer=False, accel_error="missing audio USB device")
    accel_class = Mock()
    monkeypatch.setattr(kinect1, "FreenectAccel", accel_class)
    src._open_accel()
    accel_class.assert_not_called()
    assert src.accel_status == "unavailable: missing audio USB device"


@pytest.mark.parametrize("override", [False, True])
def test_macos_local_driver_selection(tmp_path, monkeypatch, override):
    import sys
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    local = tmp_path / ".local/share/echosense/libfreenect/libfreenect.dylib"
    local.parent.mkdir(parents=True)
    local.touch()
    monkeypatch.setattr(driver.ctypes.util, "find_library", lambda name: "system-freenect")
    monkeypatch.setattr(driver.ctypes, "CDLL", Mock())
    if override:
        monkeypatch.setenv("ECHOSENSE_LIBFREENECT", "explicit-freenect")
    else:
        monkeypatch.delenv("ECHOSENSE_LIBFREENECT", raising=False)
    assert driver.find_library() == ("explicit-freenect" if override else str(local))
