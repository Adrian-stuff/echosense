# EchoSense whole-system flowchart

This maps the implemented system, including the device pipeline, authenticated parent app, supervised training, child fitting, recordings, reports, and recovery. The same software runs on an Orange Pi, a laptop, or Docker; the selected depth source and audio output differ.

Solid arrows show the main flow. Dotted arrows show optional processing, saved configuration, or recovery/control relationships. The diagrams describe software behavior, not clinical validation or guaranteed obstacle detection.

## Whole-system overview

```mermaid
flowchart TB
    START(["Start EchoSense"]) --> BOOT["Load configuration, saved fitting, and device settings"]
    BOOT --> DEVICE["Run on Orange Pi, laptop, or Docker"]
    DEVICE --> SOURCE{"Select depth source"}
    SOURCE --> KINECT["Kinect v1 or Kinect v2"]
    SOURCE --> SIM["Simulator or recorded-depth replay"]

    subgraph SENSING["Sensing and perception"]
        FRAMES["Capture depth frames and available RGB"] --> PROCESS["Filter depth; project points; estimate floor and gravity"]
        PROCESS --> DETECT["Classify and cluster obstacles and drop-offs"]
        DETECT --> TRACK["Track, recognize, filter the walking corridor, and prioritize"]
        PROCESS -.-> MAP["Optional local world map"]
        FRAMES --> HEALTH["Monitor sensor health, frame age, and performance"]
    end
    KINECT --> FRAMES
    SIM --> FRAMES

    subgraph ASSISTANCE["Audio assistance"]
        SELECT{"Select cues using power, health, and training state"}
        LIVE["Live obstacle cues"]
        VIRTUAL["Supervised virtual training target"]
        QUIET["No obstacle cues when output is disabled or sensing cannot support them"]
        AUDIO["Spatial audio synthesis, speech, mixing, and limiter"]
        CHILD["Child hears cues through open-ear or bone-conduction headset"]
        SELECT -->|"Assistive mode"| LIVE
        SELECT -->|"Powered training mode"| VIRTUAL
        SELECT -->|"Output unavailable"| QUIET
        LIVE --> AUDIO
        VIRTUAL --> AUDIO
        QUIET --> AUDIO
        AUDIO --> CHILD
    end
    TRACK --> SELECT
    HEALTH --> SELECT
    HEALTH -->|"Warnings and fault announcements"| AUDIO
    HEALTH --> RECONNECT["Watchdog retries a lost physical sensor"]
    RECONNECT -.-> KINECT

    subgraph ACCESS["Parent and caregiver access"]
        PERSON["Parent or caregiver"] --> SIGNIN["First-owner setup, invited account, or login"]
        SIGNIN --> AUTH["Validate session, request origin, CSRF, and role permissions"]
        AUTH --> WEB["Authenticated HTTP API and WebSocket server"]
        WEB <--> APP["Parent dashboard or advanced device console"]
    end
    DEVICE --> WEB
    FRAMES -->|"Depth and supported color view"| WEB
    TRACK -->|"Detections and cues"| WEB
    MAP --> WEB
    HEALTH -->|"Status and performance"| WEB
    AUDIO -->|"Optional browser listening"| WEB

    subgraph WORKFLOWS["Parent workflows"]
        FIT["Measure child; calibrate mount; preview coverage; save fitting"]
        PRACTICE["Start, pause, resume, and finish supervised practice"]
        SUPERVISOR["Device owns session, checks supervision, and acknowledges results"]
        CAPTURE["Explicitly record a clip or snapshot; add notes and session link"]
        REPORT["Review goals, sound errors, practice consistency, observations, and CSV reports"]
        ACCOUNTS["Owner invites or revokes viewers, trainers, and device managers"]
        LOCK["On detected expiry or revocation: hide private content and preserve work"]
    end
    APP --> FIT
    FIT --> SETTINGS[("Device settings JSON")]
    SETTINGS -.-> PROCESS
    SETTINGS -.-> AUDIO
    SETTINGS -.-> SELECT
    APP --> PRACTICE
    PRACTICE <--> SUPERVISOR
    SUPERVISOR -->|"Select or stop virtual sounds"| SELECT
    SUPERVISOR -->|"Disconnect, timeout, or failure"| PAUSE["Pause virtual training; return to normal cue selection; require explicit resume"]
    PAUSE --> SELECT
    PAUSE -.-> PRACTICE
    APP --> CAPTURE
    APP --> REPORT
    APP --> ACCOUNTS
    ACCOUNTS --> AUTH
    AUTH -->|"Session ends"| LOCK
    LOCK -->|"Sign in again to unlock"| SIGNIN

    subgraph STORAGE["Storage and recovery"]
        DB[("Device SQLite: accounts, hashed passwords, sessions, invitations, confirmed practice, observations")]
        LOCAL[("Browser IndexedDB: footage, profile, goals, cached history, clip notes and links")]
        DRAFT[("Browser localStorage: small recovery drafts")]
        RECOVERY["If media cannot be saved: retain in the open page and offer downloads"]
        ZIP["Portable ZIP backup or validated, duplicate-safe restore"]
        LOGS[("Optional device logs and raw-depth recordings")]
    end
    AUTH <--> DB
    SUPERVISOR <--> DB
    WEB -->|"Practice history"| REPORT
    CAPTURE --> LOCAL
    CAPTURE -->|"Storage failure"| RECOVERY
    APP <--> LOCAL
    LOCAL --> REPORT
    LOCK --> DRAFT
    LOCK -->|"Stop and save any recording"| CAPTURE
    LOCAL <--> ZIP
    RECOVERY --> ZIP
    FRAMES -.->|"Optional raw-depth recording"| LOGS
    TRACK -.->|"Optional session logging"| LOGS
    CONTROLS["Physical GPIO buttons"] -->|"Local device actions; no browser login required"| SELECT
```

## Startup and live assistance

```mermaid
flowchart TD
    A(["Start process"]) --> B["Load configuration and persistent user settings"]
    B --> C["Initialize processing, audio engine, monitors, GPIO, and web server"]
    C --> D{"Requested source available?"}
    D -->|"Yes"| E["Read next depth frame"]
    D -->|"No; device profile"| F["Report unavailable sensor and retry; do not substitute simulated sensing"]
    D -->|"No; development fallback allowed"| G["Use clearly labelled simulator and keep looking for hardware"]
    F --> E
    G --> E
    E --> H{"Frame received?"}
    H -->|"No"| I["Clear obstacle cues; update health; announce faults; attempt hardware reconnection"]
    I --> E
    H -->|"Yes"| J["Optionally save raw depth for replay"]
    J --> K["Remove noisy depth; project to body-frame points using the fitted mount"]
    K --> L["Estimate floor; use gravity or wall orientation when needed"]
    L --> M["Optional world-map update"]
    L --> N["Separate ground, obstacles, and drop-offs; cluster surfaces"]
    N --> O["Track and recognize; suppress irrelevant side surfaces; prioritize hazards"]
    O --> P{"Cue output powered on?"}
    P -->|"No"| Q["No obstacle cues"]
    P -->|"Yes"| R{"Training active?"}
    R -->|"Yes"| S["Use virtual training target"]
    R -->|"No"| T{"Sensor health permits live cues?"}
    T -->|"Starting, OK, or degraded"| U["Use selected obstacle cues; apply warning-only mode when selected"]
    T -->|"Blocked or no data"| Q
    Q --> V["Audio engine: combine cues and speech; limit output"]
    S --> V
    U --> V
    V --> W["Headset output; optional browser audio tap"]
    O --> X["Stream depth, detections, optional RGB, and status to authenticated clients"]
    M --> X
    X --> E
    W --> E
    HEALTH["Health and performance monitoring"] -->|"Fault or degraded-sensing speech"| V
    L --> HEALTH
    E --> HEALTH
```

Sensing continues while the user-facing cue output is off. Degraded sensing can continue to produce live cues while announcing a warning. The hardware watchdog runs alongside the frame loop. Battery percentages and battery warnings currently use the simulated battery model.

## Login, permissions, and child fitting

```mermaid
flowchart TD
    A["Open parent app or console"] --> B{"Existing device owner?"}
    B -->|"No"| C["Enter terminal setup code and create owner credentials"]
    B -->|"Yes"| D["Sign in, or accept a one-time caregiver invitation"]
    C --> E["Validate setup code or credentials and issue a revocable session cookie"]
    D --> E
    E --> F{"Valid session and required permission?"}
    F -->|"No session"| G["Login or privacy-lock screen"]
    F -->|"Permission denied"| H["Reject protected operation"]
    F -->|"Allowed"| I["Open authenticated dashboard and live stream"]
    I --> J["Viewer: review and download"]
    I --> K["Trainer: view, record, annotate, run practice, and restore backups"]
    I --> L["Device manager: view and configure device or fitting"]
    I --> M["Owner: all permissions, invitations, and revocation"]
    L --> N["Measure head height, ear height, sensor height, and tilt"]
    M --> N
    N --> O["Optionally calibrate against a clear floor"]
    O --> P["Preview floor, head-height, and corridor blind zones"]
    P --> Q{"Fitting checked and no active supervised practice?"}
    Q -->|"No"| R["Correct measurements or pause practice"]
    R --> N
    Q -->|"Yes"| S["Save device settings"]
    S --> T["Update processing height limits, head-level threshold, audio ear height, mount, and coverage"]
    I -->|"Detected expiry or revocation"| G
    G --> U["Hide private content; stop playback; preserve drafts and recording"]
    U -->|"Reauthenticate in place"| F
```

Authenticated HTTP mutations also check origin and a session-bound CSRF token. WebSocket upgrades check origin and authentication; device commands check permissions. Physical device buttons are local controls, independent of browser login. Password recovery is available from the device terminal.

## Supervised training and reliable results

```mermaid
flowchart TD
    A["Authorized caregiver starts practice and chooses a goal"] --> B{"Existing unfinished session?"}
    B -->|"Yes"| C["Review and explicitly resume or finish it"]
    B -->|"No"| D["Device creates session ID and assigns supervising connection"]
    C -->|"Resume"| E["Activate virtual training sounds"]
    D --> E
    E --> F["Play target with a stable trial ID"]
    F --> G["Child listens and points; caregiver enters the answer"]
    G --> H["Keep pending answer with stable response ID in browser recovery state"]
    H --> I["Device validates ownership and current trial; deduplicates retries"]
    I --> J["Calculate direction error and response time"]
    J --> K{"Persist result successfully?"}
    K -->|"No"| L["Pause virtual sounds; retain pending work; retry after recovery"]
    L --> I
    K -->|"Yes"| M["Acknowledge authoritative session results"]
    M --> N{"Next action"}
    N -->|"Another trial"| F
    N -->|"Finish"| O["Wait for any pending answer acknowledgement"]
    O --> P["Stop virtual sounds; persist final results and observation"]
    C -->|"Finish paused session"| P
    P --> Q["Update history, goals, reports, and linked footage"]
    HEART["Supervision heartbeat every 2 seconds"] --> E
    LOST["Disconnect, authentication loss, hidden tab, timeout, storage failure, or device-side stop"] --> PAUSE["Pause practice and return to normal cue selection"]
    E -.-> LOST
    PAUSE --> RESUME{"Caregiver explicitly resumes?"}
    RESUME -->|"Yes; ownership validated"| E
    RESUME -->|"Finish paused session"| P
    RESTART["Device restarts"] --> PAUSE
```

The supervision timeout is 10 seconds, checked by a watchdog every 0.5 seconds. A lost supervising WebSocket pauses on disconnect. Results, observations, and active-time checkpoints are stored on the device. A retry after a lost acknowledgement cannot add the same response twice. Training errors describe the sound exercise, not overall mobility or safety.

## Recording, backup, and privacy recovery

```mermaid
flowchart TD
    A["Authenticated trainer or owner opens the live view"] --> B{"Fresh selected camera or depth view?"}
    B -->|"No"| C["Wait for stream or recover connection"]
    C --> B
    B -->|"Yes"| D["Explicit snapshot or clip recording; optionally link a practice session"]
    D --> E["Capture image or up to 2 minutes of video without audio"]
    E --> F["Stop manually, at duration limit, or when the stream/session ends"]
    F --> G{"Save to browser IndexedDB?"}
    G -->|"Yes"| H["Library: playback, titles, notes, session links, and downloads"]
    G -->|"No"| I["Keep media in the open page; show footage and note download controls"]
    H --> J["Build ZIP: manifest, profile, goals, history, metadata, and footage"]
    I -->|"Include available recovery copies"| J
    J --> K["Download portable backup"]
    K --> L["Choose backup to restore"]
    L --> M{"Schema, checksums, paths, sizes, and IDs valid?"}
    M -->|"No"| N["Reject without restoring data"]
    M -->|"Yes"| O["Keep existing IDs; merge new records; choose whether to replace profile"]
    O --> P["Commit local state and new clips in one IndexedDB transaction"]
    P --> H
    EXPIRE["Session expiry or revocation detected"] --> LOCK["Immediately hide and disable private content"]
    LOCK --> SAVE["Stop and save recording; preserve notes and small drafts"]
    SAVE --> G
    SAVE --> REAUTH["Reauthenticate in the lock screen without reloading"]
    REAUTH --> H
```

A browser recording is separate from the device's optional raw-depth `.esd` recording. Kinect v2 currently supplies depth through its bridge; Kinect v1 and the simulator can also supply color. ZIP restore writes browser data only: it does not restore accounts, passwords, device fitting, or control authority. Imported unfinished practice is archived history, not a live session. Browser storage failure can leave footage only in page memory until it is downloaded.

## Storage and implementation map

| Location | Contents | Main implementation |
|---|---|---|
| Device `accounts.sqlite3` | Users, password hashes, roles, invitations, authenticated sessions, confirmed practice/results/observations | `echosense/auth.py`, `echosense/device/supervision.py` |
| Device settings JSON | Power/audio preferences, measured child heights, mount geometry, fitting flag | `echosense/device/settings.py`, `echosense/app.py` |
| Browser IndexedDB | Footage, profile, goals, cached history, clip metadata and session links | `echosense/web/parent-data.mjs`, `echosense/web/parent.js` |
| Browser localStorage | Small emergency draft records when available | `echosense/web/parent.js` |
| Open-page memory | Unsaved media recovery copies while storage is unavailable | `echosense/web/parent.js` |
| Downloaded ZIP/CSV/media | Portable backups, reports, and exported footage | `echosense/web/parent-data.mjs`, `echosense/web/parent-progress.mjs` |
| Optional device logs / `.esd` files | Operational session logs and replayable raw depth | `echosense/device/monitors.py`, `echosense/sensors/replay.py` |

The overall coordinator is `echosense/app.py`; `echosense/server.py` serves the authenticated app and transports live data and commands. There is currently no cloud storage, GPS tracking, or internet relay in this system. Public GitHub hosts the software source, not the child's live footage or account database.

The standalone overview source is [system-flowchart.mmd](system-flowchart.mmd). GitHub renders each Mermaid block in this document. See [parent workflows](parent-dashboard.md) and [account and permission details](authentication.md) for operating instructions.
