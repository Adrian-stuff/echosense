# Kinect v1 model 1473 accelerometer setup

Echo-Sense supports the model 1473 accelerometer with `sensor.kinect1_accelerometer`
set to its default, `"auto"`. The setup below persists across app restarts and reboots.

## macOS: one-time setup

From the repository root, using the same macOS account that runs Echo-Sense:

```sh
xcode-select --install  # only if Apple command line tools are missing
brew install libusb cmake
.venv/bin/python scripts/setup_kinect1_macos.py
```

The installer downloads Microsoft's Xbox update, extracts `audios.bin`, and builds a
patched libfreenect. If CMake is absent and `uv` is installed, it can run CMake through
`uv` instead. Stop Echo-Sense before reinstalling its driver, then restart it normally:

```sh
.venv/bin/echosense run --source kinect1
```

No shell exports or temporary build folders are needed at runtime. The installer
preserves the Homebrew libfreenect installation.

| Persistent file | Purpose |
| --- | --- |
| `~/.libfreenect/audios.bin` | Firmware that libfreenect uploads to the Kinect when needed |
| `~/.local/share/echosense/libfreenect/libfreenect.dylib` | Local macOS driver selected automatically by Echo-Sense |

The firmware file stays on the computer. After the Kinect loses power, libfreenect
loads it into the device again when opening it. Run setup again for another computer
or user account. A Homebrew libusb upgrade that makes the local driver unloadable
may require rerunning the macOS installer.

## Why this fix is necessary

Model 1473 routes accelerometer and motor controls through its audio USB device.
The camera works without audio firmware, but accelerometer access requires it.
Echo-Sense requests camera and motor controls together so libfreenect recognizes
this route, then reads acceleration through the shared device handle. It does not
start microphone recording or move the tilt motor.

On the tested MacBook Air M1/model 1473, the upstream driver's audio reset during
startup caused USB disconnections. The local build retains the already opened
audio handle instead of resetting and reopening it. This is an Echo-Sense local
workaround, not a change to the Homebrew package or an upstream libfreenect fix.

The macOS installer pins OpenKinect/libfreenect revision
`09a1f098040d00e6070c18174904547ec31d2774`, checks the source archive SHA-256,
and requires exactly one matching reset block before patching it. The firmware
extractor is also pinned and checksum-verified. Both installers stage their outputs
before replacing installed files. Source downloads and builds are setup steps;
normal app startup does not download or compile anything.

## Verify operation

1. Open the console's Fitting card and check the Accelerometer status.
2. `on` means the hardware reader opened. Keep the floor visible with the sensor
   tilted a few degrees for about a second so the app can learn the axis signs.
3. `verified` means the gravity estimate agreed with the floor. Tilt and roll should
   update as you move the sensor. Depth should continue streaming.

A fresh Python process can confirm which driver the app will load:

```sh
.venv/bin/python -c 'from echosense.sensors.freenect_ctypes import find_library; print(find_library())'
```

On macOS, expect the persistent local driver path above. An explicit
`ECHOSENSE_LIBFREENECT` environment variable takes precedence over that path;
remove an outdated override from your launch environment if it selects another driver.

Hardware validation on 2026-11-19: model 1473 produced changing three-axis readings;
the running app reported accelerometer `on`, gravity `verified`, and approximately
30 depth frames per second with the installed local driver.

## Troubleshooting

- **Missing audio/control device:** stop the app, unplug Kinect USB and power,
  reconnect power first, then USB. Both Microsoft USB products must appear:
  camera `0x02AE` and audio/control `0x02AD`. Use a direct connection or fewer hubs
  if either disappears repeatedly.
- **Firmware unavailable:** rerun setup as the account running the app. The driver
  also searches `LIBFREENECT_FIRMWARE_PATH`, the working directory,
  `/usr/local/share/libfreenect`, and `/usr/share/libfreenect` for `audios.bin`.
- **Camera busy or access errors:** close other Kinect programs before restarting.
- **Hardware on, gravity not verified:** show the floor and tilt the sensor slightly
  so axis calibration can settle. The app continues with depth-based floor estimation
  until the accelerometer is verified.
- **Controls fail to open:** Echo-Sense retries camera-only so depth can remain
  available; the console reports the accelerometer failure. This fallback does not
  mean accelerometer access succeeded.

Set `sensor.kinect1_accelerometer` to `false` to disable accelerometer access.
Setting it to `true` forces a combined camera/control attempt, but does not replace
missing firmware or fix a missing USB device.

## Linux and device services

Use the firmware installer without the macOS driver workaround:

```sh
.venv/bin/python scripts/setup_kinect1_firmware.py
```

For an existing firmware file, add `--firmware /path/to/audios.bin`. For a service
running under another account, install into a driver search directory accessible to
that account, for example using `--output /usr/local/share/libfreenect/audios.bin`
with the appropriate filesystem permissions. Then restart the service. Linux still
requires working libfreenect and USB permissions for camera and audio/control devices.

## Regression checks

```sh
.venv/bin/pytest -q tests/test_kinect1_firmware.py
```

These tests cover firmware discovery, combined camera/control opening, camera-only
fallback, shared-handle ownership, settings, and persistent macOS driver selection.
They do not replace hardware verification after changing libfreenect or USB dependencies.
