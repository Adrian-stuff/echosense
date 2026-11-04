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
* **Kinect v1** (Xbox 360) works too if libfreenect's Python `freenect` module is installed.
* **Record and replay**: `echosense run --source kinect2 --record street.esd`, later
  `echosense run --source replay:street.esd` (on the laptop or in the virtual Orange Pi).

**Do this first with a real sensor:**
1. **Mirror check.** Stand in front of the Kinect and raise your right hand. In the console's Sensor
   view it must appear on the right. If it is on the left, untick/tick *Mirror Kinect image*
   (Fitting card) and set `sensor.flip_horizontal` in your profile. A mirrored image would send
   every cue to the wrong ear.
2. **Fitting.** Mount it on the chest, face a clear floor and press *Calibrate from floor*. This
   stores the real mount height and tilt for this user.
3. **Tilt trade-off.** The Kinect's vertical FOV is 60°. At −10° tilt the floor is visible from about
   1.5 m and head-level objects from about 1.5 m outward. Tilting down sees drop-offs nearer but loses
   head-level coverage. Try it with the *Mount tilt* slider in the simulator before deciding.

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
   the sidewalk doesn't win.
3. **Obstacle clustering**: done separately inside and outside the walking corridor. Walls and curbs
   running parallel to the path are suppressed. Drop-off distance is measured at the lip.
4. **Distance estimate**: a per-column front-face estimate. Noise averages out instead of pulling
   the distance toward the sensor, and any remaining bias is toward the user (the safe side).
5. **Tracking**: an alpha-beta filter on range estimates closing speed and **time-to-contact**. A
   person walking toward you is prioritised and pinged by how soon you'd meet, not just distance.
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
