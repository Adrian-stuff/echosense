# Echo-Sense — device software, simulator and virtual Orange Pi

Software for the Echo-Sense assistive device: a chest-mounted **Kinect v2** depth sensor
and an **Orange Pi** that turn obstacles in front of a visually impaired user into
**3D (HRTF) spatial audio** cues. The same program runs in three places:

| Where | Depth comes from | Audio goes to | Command |
|---|---|---|---|
| Laptop (development) | simulator, or a Kinect v2 plugged into the laptop | laptop speakers / headphones | `.venv/bin/echosense run` |
| Virtual Orange Pi (Docker, arm64 Ubuntu 22.04) | simulator, recorded sessions, Kinect on a Linux host | browser ("Listen") | `docker compose up --build` |
| Real Orange Pi | Kinect v2 over USB 3.0 | open-ear / bone-conduction headset | `systemctl start echosense` |

Every setup serves the same **web console** (`http://localhost:8080`, or `http://<orange-pi-ip>:8080`
over Wi-Fi). It has a live 3D point cloud of what the sensor sees, the depth image, a top-down
cue map, the device buttons, simulator controls, fitting/calibration, training mode and session logging.

```
 Sensing ─────────────► Processing ─────────────────────────► Audio rendering ──► headset
 sim / Kinect v2 /      body-frame point cloud → RANSAC floor   HRTF (Brown–Duda), pitch = height band,
 Kinect v1 / replay     → ground / obstacle / drop-off → grid   repetition + loudness = distance,
 30 Hz, 512×424         clusters → corridor filter → tracker    output limiter (−6 dBFS), speech
                        (false-positive suppression) → priority
                                     ▲
 Power & control: buttons (GPIO / console / keyboard), settings that persist, battery, sensor-health
 fail-safe alarm, self-test announcement, training mode, session logging
```

The four modules match the proposal's System Architecture (section 7-D). The code is in
`echosense/sensors`, `echosense/processing`, `echosense/audio`, and `echosense/device` + `echosense/app.py`.

## Parent dashboard

Open **[Parent space](http://localhost:8080/parents)** alongside the existing console to view the sensor, record clips and snapshots, annotate footage, and save supervised stationary training sessions. Footage, profiles, and goals stay in the current browser; confirmed training results and observations are also saved on the device. ZIP backup/restore keeps footage linked to practice history. The dashboard labels simulated readings and supports depth-only devices. See [the parent dashboard guide](docs/parent-dashboard.md) for storage limits and recommended next steps.

## Parent login

The console and parent dashboard now require login. On first launch, open [account setup](http://localhost:8080/login), enter the setup code printed by EchoSense, and choose your username and password. The setup code is required only for the first account. Sign out from either app; change your password in **Profile & privacy**. For local password recovery, run `.venv/bin/echosense reset-password`.

Accounts are stored on the device. The owner can invite viewers, trainers, and device managers with separate permissions. Supervised practice pauses on lost supervision, and measured child fitting updates sensing, audio, and blind-zone previews. Existing clips and notes stay in your browser. See [authentication setup and deployment](docs/authentication.md) for storage paths, HTTPS configuration, and recovery.

## Quick start (macOS / Linux laptop)

```bash
uv venv --python 3.10 .venv            # 3.10 = the Orange Pi's Ubuntu 22.04 Python
uv pip install --python .venv -e ".[dev]"
.venv/bin/echosense run                 # opens the console; uses a Kinect if one is found, else the simulator
```

In the console:

* **Walk**: W/S forward/back, A/D sidestep, Q/E or ←/→ turn. Or tick **Auto-walk route**.
* **Device buttons**: P power, +/− volume, R range (near 1.5 m / medium 2.5 m / far 4 m), M mode
  (continuous / warning-only), O sound profile, B battery, I status, T training.
* **Views**: Follow, Sensor view (what the Kinect sees), Top, Free (orbit with the mouse). V cycles them.
* Use **headphones**. The direction cues come from interaural time and level differences.

### How the sound encodes an obstacle

| | Cue |
|---|---|
| Direction | rendered through the HRTF at the obstacle's azimuth/elevation |
| Distance | nearer = faster pings and louder, rapid burst under 0.6 m |
| Body-height obstacle | mid tone (620 Hz) |
| Low obstacle (tripping hazard) | low tone (360 Hz) |
| Head-level overhang (cane can't detect) | high double-ping (1150 Hz) |
| Drop-off (open drainage, curb, hole) | falling sweep |

Only obstacles inside the walking corridor (±0.45 m) are reported, nearest first, at most two at a
time (the second is quieter). Walls running parallel to the path are suppressed. An obstacle must
be seen in 3 consecutive frames (2 on the Orange Pi profile) before it sounds.

## Kinect v2

The Kinect v2 needs the **Kinect Adapter for Windows** (USB 3.0 + 12 V), and Microsoft's SDK is
Windows-only, so Echo-Sense uses **libfreenect2** through a small native helper,
`native/kinect2_bridge` (`echosense-kinect2`). It streams depth frames to Python over a pipe, so no
fragile Python bindings are needed.

* **macOS**: `scripts/setup_kinect2_macos.sh`, then `.venv/bin/echosense run --source kinect2`
  (libfreenect2's own `~/.local/freenect2/bin/Protonect` is a quick hardware check)
* **Orange Pi / Linux**: `sudo deploy/install_orangepi.sh` (builds libfreenect2, udev rules, bridge, app, service)
* **Without hardware**: `echosense run --source kinect2-fake` exercises the whole bridge path with synthetic frames.
* The console's source switcher (Simulator / Kinect v2 / Kinect v1) changes source live.
  `--source auto` (the default) picks a Kinect when one is connected.
* **Kinect v1** (Xbox 360): `brew install libfreenect` (macOS) or `apt install libfreenect0.5`
  (Debian/Armbian), then `echosense run --source kinect1`. Echo-Sense opens only the depth camera
  through a small ctypes binding, so no Python wrapper needs compiling and newer Kinects (model 1473)
  don't need firmware for depth sensing. Tested on a MacBook Air M1: 30 fps, processing p95 11–17 ms.
* **Record and replay**: `echosense run --source kinect2 --record street.esd`, later
  `echosense run --source replay:street.esd` (on the laptop or in the virtual Orange Pi).

**Do this first with a real sensor:**
1. **Mirror check.** Stand in front of the Kinect and raise your right hand. In the console's Sensor
   view it must appear on the right. If it is on the left, untick/tick *Mirror Kinect image*
   (Fitting card) and set `sensor.flip_horizontal` in your profile. A mirrored image would send
   every cue to the wrong ear.
2. **Fitting.** Mount it on the chest, face a clear floor and press *Calibrate from floor*. This
   stores the real mount height and tilt for this user. If you skip this, Echo-Sense still finds the
   floor on its own (on a desk, in the hand, strapped higher or tilted more) and says so in the
   event log. Before this, a sensor more than about 15 cm or 5° away from the profile's mount turned
   the whole floor into one obstacle that beeped all the time.
3. **Sensitivity** (Fitting card): how sure Echo-Sense must be before it sounds an obstacle.
   * *Normal* (default): seen in 4 frames and for 0.1 s, in at least 70% of frames since it
     appeared; blobs under 8 points ignored; the floor band widens by 2 cm per metre beyond 2.5 m.
   * *Low*: 6 frames and 0.25 s, 80% of frames, larger blobs only, a narrower corridor, and one
     obstacle sounding at a time. Quietest. A 10 cm curb at the very bottom of the Kinect v1's view
     is caught less often.
   * *High*: the old behaviour (3 frames). Earliest cues, most false alarms.
   The levels are in `sensitivity_levels` in `config.py`.
4. **Tilt trade-off.** The Kinect's vertical FOV is 60°. At −10° tilt the floor is visible from about
   1.5 m and head-level objects from about 1.5 m outward. Tilting down sees drop-offs nearer but loses
   head-level coverage. Try it with the *Mount tilt* slider in the simulator before deciding.

### If the Kinect drops out

A Kinect can fall off USB for a moment (a hub hiccup, a loose cable, a power dip). libfreenect then
logs "USB camera marked dead" and stops streaming. Echo-Sense watches for this:
* no depth for 1.5 s, or the driver reporting the device lost, makes it say *"Sensor not
  responding. Use your cane."*
* it reopens the Kinect every couple of seconds until the stream is back, then says *"Sensor OK"*
  (tested on a Kinect v1: back in about 3 s)
* plugging the Kinect in later is enough; no restart needed

On the device profiles (`orangepi5`, `orangepi_one`) a missing Kinect is never replaced by the
simulator (`allow_sim_fallback: false`): fake depth must not drive real cues. On a laptop it falls
back to the simulator and switches to the Kinect when it appears. Fewer USB hops help: plug the
Kinect into the computer directly or into a powered hub, not through a chain of hubs.

### Camera view and near mode

* **Camera** card: the colour picture (Kinect v1 colour camera; in the simulator, a rendered view in
  material colours) with every detected obstacle boxed and labelled. *Camera colours* paints the
  3D point cloud with the real colours. The colour stream only runs while someone is watching it,
  so the device pays nothing for it otherwise. The Kinect v2 bridge streams depth only for now.
* **Full resolution** draws every depth pixel (640×480 on a Kinect v1) instead of every second one.
  Processing already uses the sensor's full resolution and frame rate: nothing is clipped. The
  Fitting card shows the live nearest and farthest depth.
* **Near mode** (Fitting card):
  * Asks the sensor for hardware near mode (0.4–3 m). Only the Kinect *for Windows* has it. The
    Xbox 360 Kinect refuses it (tested: `freenect_set_flag` returns −1). It already reports down to
    about 0.45 m, and nothing closer.
  * Adds close-range memory on any sensor: an obstacle in the path that comes closer than about
    0.9 m and then vanishes into the blind zone keeps sounding as "very close" for 1.5 s, instead
    of going silent the moment it matters most.

### Accelerometer (Kinect v1)

For model 1473, see the [permanent setup and troubleshooting guide](docs/kinect1473.md)
for installation, reboot behavior, verification, and driver maintenance.

The Kinect v1 has an accelerometer, which gives the sensor's true tilt (and roll) every frame
(`processing/gravity.py`):
* **No floor in view** (up close to a wall, in a crowd): heights are kept level from gravity at
  the height the floor was last seen. Before, the estimate fell back to the configured mount after
  2 s. In the simulator with a 0.9 m / −4° sensor 1 m from a wall, the wall's height band is
  0.4–1.2 m (correct) instead of 0.7–1.4 m.
* **Floor fit**: only surfaces within 15° of level count as the floor (not a table edge or a
  car bonnet). A ramp still works.
* **Pointing warning**: "Sensor is pointing at the floor / up. Check the strap." after a second
  beyond −45° or +25° (`sensor.pointing_floor_deg` / `pointing_sky_deg`).
* The Fitting card shows the accelerometer's tilt and roll, and "floor from gravity" when no floor
  is in view.

How libfreenect's accelerometer axes line up with the depth image isn't documented (and the
mirror setting flips one), so **the axis signs are learned** by comparing with the fitted floor
over about 20 frames. Nothing uses the accelerometer until it has agreed with the floor; until
then the device behaves exactly as without it. Keep the floor in view, with the sensor tilted a
few degrees, for a second after start-up.

Which Kinects have it:
* **Model 1414** has a separate motor/accelerometer USB device (product id 0x02B0). It is opened
  in its own libfreenect context, read 20 times a second, and never moves the motor.
* **Model 1473** keeps the motor and accelerometer behind its audio chip, which needs
  Microsoft's audio firmware (`audios.bin`, extracted by libfreenect's `fwfetcher.py` from an Xbox
  update) uploaded first. Install it once, then restart Echo-Sense:

  ```sh
  .venv/bin/python scripts/setup_kinect1_firmware.py
  ```

  On macOS, use `.venv/bin/python scripts/setup_kinect1_macos.py` instead. It installs the
  firmware and builds a local libfreenect with the audio USB reset removed: on the tested
  MacBook Air M1/model 1473, that reset disconnected the audio/control device during startup.
  It needs Homebrew libusb and CMake (or `uv` to run CMake). Echo-Sense automatically selects
  the local build from `~/.local/share/echosense/libfreenect/libfreenect.dylib`; the Homebrew
  installation is untouched. `ECHOSENSE_LIBFREENECT` still overrides the driver selection.

  This downloads Microsoft's Xbox update and extracts the firmware with OpenKinect's pinned,
  checksum-verified extractor into `~/.libfreenect/audios.bin`. For an offline install, pass
  `--firmware /path/to/audios.bin`. Run it as the account that runs Echo-Sense, or install to
  `/usr/local/share/libfreenect/audios.bin` with `--output` for a system service.
  Default `sensor.kinect1_accelerometer: "auto"` now opens camera and motor controls together
  when firmware is installed; libfreenect uploads it on connection. It streams no microphone
  audio and never moves the motor. If controls cannot open, depth continues without them.
  Set the option to `false` to disable accelerometer access, or `true` to force an attempt
  (including firmware embedded in a custom libfreenect build).
  If the status reports a missing audio/control device, unplug both Kinect power and USB,
  reconnect power first, then USB. Both camera and audio/control devices must appear on USB.
* Kinect v2: libfreenect2 doesn't expose its IMU.

## Virtual Orange Pi (Docker)

```bash
docker compose up --build               # first build compiles libfreenect2, a few minutes
open http://localhost:8080              # same console; press 🎧 Listen to hear the device audio
docker compose run --rm orangepi echosense bench                                   # timing on arm64
docker compose run --rm orangepi /opt/echosense/venv/bin/pytest -q /opt/echosense/src/tests
docker compose exec orangepi bash       # a shell "on the device" (user orangepi, host orangepi5)
```

What it reproduces:
* arm64 CPU architecture (native on Apple Silicon, QEMU-emulated on x86)
* Ubuntu 22.04 / Python 3.10, like the Orange Pi 5's official image
* the same install script as the real board (`deploy/install_orangepi.sh`)
* the `orangepi5` profile and the systemd service command
* 4 CPU / 4 GB caps (`OPI_CPUS`, `OPI_MEM`)
* persistent home volume for settings and logs

What it can't reproduce:
* **Per-core speed.** Docker can cap the core count, not the clock. On the development MacBook Air
  (M1), Docker Desktop's VM ran the pipeline about 3× slower than native macOS: processing averaged
  10–15 ms with a 40–55 ms p95, and obstacle-to-sound averaged 170 ms with a worst case near 320 ms.
  Those numbers aren't the Orange Pi's in either direction. Use them to compare builds; take the
  real figures on the board.
* **A sound card.** Audio runs on a clock; use *Listen*.
* **USB passthrough on macOS/Windows Docker Desktop.** On a Linux host add
  `-f docker/docker-compose.kinect-linux.yml` to pass a real Kinect in.

## Depth sensing

The processing stages from depth frame to obstacle (`echosense/processing/`):

1. **Front-end** (`depthfilter.py`):
   * removes mixed ("flying") pixels at object edges
   * downsamples each block to its nearest surface, so a 5 cm pole at 4 m or a 3 cm cable isn't skipped
   * holds flickering pixels for 2 frames, so sun or dark-surface dropouts don't make obstacles blink
2. **Floor fit**: RANSAC that prefers the surface the user is standing on, so a street 15 cm below
   the sidewalk doesn't win. If no floor is found near the configured mount, it searches the whole
   view for the lowest large level surface (at most every 0.4 s) and follows that instead.
   "Level" is checked against the accelerometer when there is one, else against the walls: indoors,
   two large walls that aren't parallel are both vertical, which fixes "up" with no floor in view.
   A surface that isn't level by that reference is never taken for the floor. Tested on a real
   Kinect v1 sitting level on a desk with no floor in view: a sloping piece of furniture used to be
   fitted as the floor, and the room filled with phantom holes. When no floor is in view, heights
   are kept level from gravity or the walls instead of falling back to the configured mount.
3. **Obstacle clustering**: done separately inside and outside the walking corridor. Walls and curbs
   running parallel to the path are suppressed. Drop-off distance is measured at the lip.
4. **Distance estimate**: a per-column front-face estimate. Noise averages out instead of pulling
   the distance toward the sensor, and any remaining bias is toward the user (the safe side).
5. **Tracking**: an alpha-beta filter on range estimates closing speed and **time-to-contact**. A
   person walking toward you is prioritised and pinged by how soon you'd meet, not just distance.
   * Each obstacle is matched to where its track *should* be by now (moved along its bearing at
     its closing speed), so fast approaches don't break a track into a new one.
   * Changes that make an obstacle more of a hazard apply at once (it enters the path). Changes
     that would quiet it or change its sound (it leaves the path, looks like a wall beside the path,
     body ↔ low ↔ head) must hold for 2–3 frames, so the pitch doesn't flicker as the shape wobbles.
   * The obstacle sounding first keeps that place until another is at least 0.3 m more urgent
     (`rank_hysteresis`), so two similar obstacles don't trade the lead cue every few frames.
6. **Sensing quality**: the share of floor-facing pixels that return depth. Below 75% for 1.5 s
   (strong sun, dirty lens) the device says *"Depth sensing degraded. Take extra care."* Cues continue.

`echosense eval-sensing` runs the hard cases (thin poles, cables, head-level sign, curb, step down,
foliage and concrete in sun, dark fabric, glass, an approaching pedestrian). Results on the
Orange Pi profile:

| | Before | After |
|---|---|---|
| Mean distance error | 4.3 cm | 2.5 cm |
| Frame-to-frame jitter (unsteady ping rate) | 0.5 cm | 0.1 cm |
| Foliage in direct sun at 4 m | 16.3 cm (fails 15 cm) | 12.9 cm, reported nearer |
| Thin 5 cm pole at 4 m | 4.7 cm | 0.7 cm |
| Approaching pedestrian, reported vs true | 3.7 cm farther | 0.1 cm |

The simulator's Kinect model was also made harsher for this, so the gains aren't tuned to an easy
sensor:
* sunlight blanks whole patches of the image, not scattered pixels
* dark fabric and foliage lose returns with range
* the simulated Kinect v1 has structured-light noise: depth comes in disparity steps (about 2.6 cm
  at 3 m, 4.6 cm at 4 m), and the error is correlated in patches, not independent per pixel

## Field of view

The Kinect v2's field of view (70° × 60°) is fixed by its optics. What the software offers:

* **Detection zone** (Fitting card): *Walking corridor* (default, as in the proposal) or *Full field
  of view*, which also cues obstacles to the sides, quieter and after the ones in the path.
* **Coverage figures** (Fitting card): where the sensor is blind for the current sensor and mount.
  With the Kinect v2 at 1.25 m and −10° tilt, the floor is visible from 1.48 m, head height (1.7 m)
  from 1.23 m, and the full corridor width from 0.64 m. Closer than that, the cane has to cover it.
* **Other sensors in the simulator** (Simulator card, or `echosense run --sensor NAME`), to decide
  whether a wider sensor is worth it before buying:

| Sensor | FOV | Range | Floor seen from | Head height seen from | Width at 2 m |
|---|---|---|---|---|---|
| Kinect v2 (`kinect_v2`) | 70 × 60° | 0.5–4.5 m | 1.48 m | 1.23 m | 2.8 m |
| Azure Kinect / Orbbec Femto narrow (`azure_nfov`) | 75 × 65° | 0.5–3.9 m | 1.36 m | 1.09 m | 3.1 m |
| Azure Kinect / Orbbec Femto wide (`azure_wfov`) | 120 × 120° | 0.25–2.9 m | 0.45 m | 0.38 m | 6.9 m |
| RealSense D455 (`realsense_d455`) | 87 × 58° | 0.6–6 m | 1.54 m | 1.31 m | 3.8 m |

(Figures are at 1.25 m mount height and −10° tilt.) The wide mode removes almost all close-range
blind zones but only reaches 2.9 m, and it can't see into a 60 cm-deep drain until about 2 m. The
pipeline picks a finer stride automatically for coarser sensors so thin cables are still caught.
Only the Kinect v2 has a hardware driver here; the others are simulation models, with wide lenses
treated as pinhole.

* **Viewer FOV** slider (under the 3D view) widens the 3D camera. *Sensor view* always uses the
  sensor's real FOV.

## World map (prototype)

Each depth frame only shows what is in front of the sensor right now. `processing/worldmap.py`
keeps a **rolling top-down map** (16 × 16 m, 5 cm cells) of what was seen while walking, so a pole
just passed or a curb that dropped out of view below is still known. The console's **World map**
card shows it north-up around you with the path walked; in the simulator, the true path is drawn
dashed for comparison. The map **does not change the audio cues yet**.

The Kinect v2 has no motion sensor and the v1's accelerometer only gives "up", so the map finds how
you moved from the depth itself (scan matching): it levels each frame with the fitted floor, takes
the wall / pole / car points 0.25–2 m above it, and lines them up with the map (Gauss–Newton on a
distance field, starting from a walking motion model). Things that move away fade out of the map.

```bash
echosense eval-map                               # estimated vs true pose along each scene's route
echosense --profile orangepi_one eval-map --preset kinect_v1
```

Simulator results so far (one run per route, with chest bob):

| Sensor | Street (30 m) | Obstacle course (12 m) | Open area | Map time |
|---|---|---|---|---|
| Kinect v2, 30 fps | 9.8 % drift | 7.3 % drift | can't track | ~3 ms/frame |
| Kinect v1, 15 fps | 24.5 % drift | 14.0 % drift | can't track | ~3 ms/frame |

Walking straight with a wall or poles in view it stays within a few cm per metre. Most of the drift
comes from **turns made while nothing is in view** (turning towards an empty street, the open area):
there's nothing to line up, the heading is guessed, and every later metre inherits that heading
error. A cheap gyro (e.g. an MPU-6050 / BMI160 on the Orange Pi's I2C pins) would measure exactly
those turns. Off by default in the `orangepi_one` profile (the board has no CPU to spare for a
console-only feature); turn it on with `"processing": {"world_map": {"enabled": true}}`.

## Object recognition (depth only, lightweight)

Each obstacle gets a type: **person, pole, vehicle, wall, object** (stalls, boxes, bins, cases),
**low obstacle, overhang, hole, step down, curb**. It works from the depth shape alone: no camera,
no GPU, no extra hardware. It runs in the dark and raises no privacy issue.

* **Overhangs and low obstacles are named by geometry.** Something with free space below that
  the sensor actually saw is an overhang; something entirely below knee height is a low obstacle.
  "Actually saw" excludes space hidden by the object itself or by something lower in front of it
  (the top of a parked motorcycle used to be called an overhang), and the far range where the
  depth limit cuts off the bottom of a wall.
  Holes vs step-downs go by depth, and curbs by their parallel edge.
* **Everything else goes to a small random forest** (`echosense/processing/models/objects.json`:
  9 trees, 167 KB, about 20 µs per obstacle) over 18 shape numbers: width at three heights,
  height span, solidity, surface density and roughness, how flat the visible surface is and
  whether it faces you or runs along your line of sight, and whether the field of view cuts it off
  at the top, bottom or **sides** (half a wall at the edge of the view is not a person). Labels
  settle over about 5 frames and only change when another class is clearly more likely. Below 50%
  confidence (`label_min_conf`) it says just "Obstacle": better unnamed than misnamed.
* **Sounds don't change.** Cues still encode height band and distance, as the proposal specifies.
  The type is **spoken on demand**:
  * the **What's ahead?** button (Space) says e.g. "Person, 2 meters, slightly right. Pole, 3
    meters, ahead." (up to three obstacles, those in the path first)
  * optionally, **"Announce people and vehicles automatically"** (Fitting card) says "Person
    ahead" when one appears
* **Retrain** with `echosense train-objects --trees 9`. It generates about 2,800 random scenes
  (people standing and walking, poles, trunks, cars, tricycles, motorcycles, walls, gates, stalls,
  bins, curbs, benches, signs, cables, awnings, canopies) with random sizes, materials, sunlight and
  mount, seen by Kinect v2 and v1 models. Half the scenes are cluttered (objects close together and
  partly out of view) and half are walked through with chest bob, and mounts range from a chest
  strap to a desk (0.7–1.55 m, −25° to +5°). It takes about 20 minutes on 8 cores and writes a
  report; `--cache rows.json` keeps the generated rows so retraining with other settings takes
  seconds. `--extra real.csv` adds labelled rows from field recordings.

Accuracy in simulation (street and course are walked along their routes and never used for training):

| Test | Old (one tree) | Now (forest) |
|---|---|---|
| Street walk-through: correct / unsure / **wrong** | 56–60% / 6–8% / **31–33%** | 61–63% / 20–21% / **18%** |
| Obstacle course: correct / wrong | 95% / 3–4% | 92–96% / 1.5–2% |
| Held-out random scenes | 87% (easy scenes) | 79% (cluttered, walking: harder) |

Its weak point is still parked vehicles vs stalls: a tricycle or a side-on motorcycle is a block
of the same size as a stall, and that is most of the remaining street errors. A camera would settle
that; see "Next steps" below. The model is trained on simulator shapes, so it **must be checked
against labelled Kinect recordings** before anyone relies on the spoken types. Obstacle warnings
never wait for recognition.

## Orange Pi One

**The Kinect v2 cannot run on an Orange Pi One.** The One has only USB 2.0, and the Kinect v2
needs USB 3.0. Even with the right port, the One's 32-bit Cortex-A7 cores couldn't decode Kinect v2
depth in real time.

**An Orange Pi One with a Kinect v1 (Xbox 360) is supported** through the `orangepi_one` profile.
The Kinect v1 runs on USB 2.0 and computes depth inside the sensor, so the board does very little
decoding. The profile runs at 15 fps (the proposal's minimum) with coarse sampling and 24 kHz
audio. Install it on the board with:

```bash
sudo deploy/install_orangepi_one.sh                         # Armbian / Debian 12 armhf, all from apt
echosense --profile orangepi_one perf --source kinect1 --minutes 5
```

On the development Mac the lite pipeline takes about 5 ms per frame (p95 14 ms in the live app,
16 fps). A Cortex-A7 at 1.2 GHz is roughly 10–15× slower per core, so expect 50–80 ms per frame on
the One: probably workable at 15 fps but with little headroom. Only `echosense perf` on the board
will tell. If it's too slow, lower `sensor.fps` to 10, raise `processing.stride` to 10, or set
`web.enabled` to `false`.

Compatibility is checked: all tests pass with Debian 12's armhf library versions (Python 3.11,
numpy 1.24, aiohttp 3.8, psutil 5.9). A 32-bit ARM container exists
(`docker compose --profile one up --build orangepi-one`, port 8082) but hasn't been run yet. It
uses QEMU emulation on Apple Silicon, so it checks installation, not speed.

What changes with Kinect v1 + Orange Pi One, compared with the proposal's Kinect v2 + Orange Pi 5:

| | Kinect v2 | Kinect v1 |
|---|---|---|
| Field of view | 70 × 60° | 57 × 45° |
| Range | 0.5–4.5 m | 0.8–4.0 m |
| Floor visible from (1.25 m, −10° tilt) | 1.48 m | ~2 m (a 15 cm step-down at 2 m is missed) |
| Head height (1.7 m) visible from | 1.23 m | ~2 m (a 3 cm cable at 2 m is missed) |
| Direct sunlight | degrades (ToF, sunlight suppression) | **structured light: largely fails outdoors in direct sun** |

The sunlight row matters most for a device meant for barangay streets. Plan the field trials for
shade or overcast conditions, or keep a USB 3.0 board. The board itself also has practical limits:
* 512 MB RAM. Echo-Sense uses about 60 MB.
* No Wi-Fi. Use Ethernet for the caregiver console.
* Usually no headphone jack. Plan on a USB audio adapter.
* One USB host port. A USB hub is needed for both the Kinect and the audio adapter.

If cost is the reason for the One, a cheaper board that still has USB 3.0 (e.g. Orange Pi 3B)
keeps the Kinect v2. Its slower cores need checking with `echosense perf`.

## Can the Orange Pi handle it? (performance monitor)

The device software samples itself every second. The console's **Performance** card and the
`perf:` pill in the top bar show a verdict (*Can handle it* / *Marginal* / *Overloaded*), 3-minute
trend lines, per-core CPU bars, and these checks:

| Check | Pass |
|---|---|
| Frame rate | ≥ 15 fps (proposal's sensor requirement) |
| Keeping up with the sensor | ≤ 5 % of frames dropped |
| Processing time p95 | ≤ half a frame (17 ms at 30 fps) |
| Obstacle-to-sound latency p95 | ≤ 300 ms |
| Audio dropouts | none in the last 60 s |
| Audio thread load | ≤ 40 % |
| Device software CPU (Echo-Sense + Kinect driver) | ≤ 60 % of one core |
| Busiest core / total CPU | ≤ 80 % / ≤ 70 % (graded only on the board with a real sensor) |
| SoC temperature | ≤ 75 °C (RK3588 throttles near 85 °C) |
| CPU clock throttling | none |
| Free memory / device memory | ≥ 25 % / ≤ 400 MB |

CPU is split by role: device software, the Kinect driver process (`echosense-kinect2`), and the
simulator. The simulator's share is excluded from the verdict because it doesn't exist on the
device. In simulator runs, the whole-machine CPU checks are shown for information only.

On the board, run it headless over SSH:

```bash
echosense perf --source kinect2 --minutes 10     # the real answer: Kinect decoding + pipeline + audio
echosense perf --source kinect2 --minutes 60     # endurance / thermal run inside the housing
echosense perf --minutes 1                       # quick check with the simulator
```

It prints a line every 5 s, then the worst result per check and a verdict. It writes every sample
to `reports/perf-<host>-<time>.csv` and exits with 0 (handles it), 1 (marginal) or 2 (overloaded).
`echosense run --perf-csv FILE` records the same samples during normal use, and session logs
include them when logging is on.

Numbers so far:

| Machine | Source | fps | Processing p95 | Latency p95 | Device CPU | Verdict |
|---|---|---|---|---|---|---|
| MacBook Air M1 (dev) | simulator | 28 | 9 ms | 112 ms | 14 % of a core | Can handle it |
| Virtual Orange Pi (Docker, 4 CPU cap) | simulator | 22 | 8 ms | 141 ms | 13 % of a core | Can handle it |
| Orange Pi 5 | Kinect v2 | not measured yet: run `echosense perf --source kinect2` | | | | |

The missing piece is the Kinect v2 driver. libfreenect2's CPU depth decoder is the heaviest part of
the device, and only the board can measure it.

## Development tools

```bash
echosense bench                 # per-stage timing + estimated obstacle-to-sound latency vs the 300 ms budget
echosense protocol --quick      # lab protocol in simulation (concrete, indoor, 5 reps), a few seconds
echosense protocol              # full: 8 distances x 9 azimuths x 5 materials x 3 lighting x 30 reps
echosense eval-sensing          # hard-case depth benchmark (--preset azure_wfov etc. to compare sensors)
echosense eval-map              # world map: scan-matching pose vs simulator truth along each route
echosense train-objects         # retrain the depth-only object recogniser (writes reports/objects-*.md)
echosense info                  # what this machine has: sources, audio device, TTS
.venv/bin/pytest -q             # test-suite
```

`protocol` writes `reports/protocol-*.csv` (every trial) and a Markdown summary checked against the
acceptance thresholds (MAE ≤ 15 cm, detection ≥ 90 %, latency ≤ 300 ms, plus the false-positive rate).
The simulator's noise model approximates a Kinect v2. Use it to rehearse the procedure and catch
regressions; thesis numbers must come from the real sensor.

Scenes live in `echosense/sim/scenes/*.json` (boxes, vertical cylinders, spheres, pits, walking
pedestrians, with materials concrete / metal / glass / fabric / foliage / wood). The street scene
models the locale's hazards: pole, low sign, tricycle, drainage, bench, tree canopy at head height,
motorcycle, glass case, hanging cable, oncoming pedestrian, curb. Copy one to build a trial course.

## Proposal functions → code

| Proposal | Where |
|---|---|
| A1 power, A2 self-test announcement | `app.py` `action("power")`, `_self_test` |
| A3/A4 forward, chest and head-level detection | `processing/pipeline.py` (kinds `body`, `head`, `low`, `drop`) |
| A5/A6 directional cue, distance variation | `audio/hrtf.py`, `audio/sonify.py` |
| A7/A8/A9 range, mode, volume | `device/settings.py`, `app.py` actions |
| A10 battery inquiry, C10 low-battery warning | `device/monitors.py` `Battery` (simulated until the fuel gauge is chosen) |
| A11/B6 training mode, angular error | `device/training.py`, console Training card |
| A12/C11 settings retained | `~/.echosense/settings.json` (atomic writes) |
| B1/B2 fitting, initial configuration | console Fitting card, *Calibrate from floor* |
| B3 audio profile | sound profiles `tone` / `click` / `soft` |
| B7/C12 session logs | `device/monitors.py` `SessionLogger` → `logs/session-*.jsonl` (no personal data) |
| C1 depth acquisition | `sensors/` (sim, `kinect2.py` + `native/kinect2_bridge`, kinect1, replay) |
| C2/C3/C4/C7 bearing, filtering, priority, false-positive suppression | `processing/pipeline.py` |
| C6 latency budget | console *e2e* readout, `echosense bench` |
| C8 sensor health fail-safe | `device/monitors.py` `HealthMonitor` (blocked lens / no data → alarm) |
| C9 output level limiting | `audio/engine.py` peak limiter at `audio.max_output_dbfs` |
| Physical buttons | `device/gpio.py` (libgpiod; set pins in the profile's `gpio.buttons`) |

## Not done yet / needs hardware

* **Camera-based recognition (next step).** Kinect v2 colour + a YOLO-nano detector on the Orange
  Pi 5's NPU (RK3588), fused with depth, would tell tricycles from stalls and recognise signs. It
  needs the board, colour streaming in the bridge, locally collected training photos, and
  privacy handling (process on device, never store frames). It isn't possible on an Orange Pi One.

* Kinect v2 path compiled and protocol-tested with `--fake`, but **not yet run against a real Kinect**.
  Do the mirror check above first.
* **HRTF** is the parametric Brown & Duda model, so it needs no data files. Swapping in a measured
  HRTF (SOFA file) behind `hrir(az, el)` is a natural Sprint 2 experiment.
* **Battery** is simulated and **GPIO buttons** are untested until the power/control hardware is chosen (Sprint 3).
* **Speech** is English via espeak-ng / macOS `say`. Phrases are in `config.py` → `phrases`, ready
  for a Filipino set.
* **Kinect decoding cost.** libfreenect2's CPU depth decoder is heavy on an Orange Pi. Try
  `--opencl` (Mali OpenCL) in the install script on the board and measure with `echosense bench`
  plus the console's fps.
