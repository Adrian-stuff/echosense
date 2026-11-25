# Parent dashboard

Open `/parents` on the existing EchoSense device server. The original console at `/` also links to **Parent space**. No additional service or runtime dependency is required.

```sh
.venv/bin/echosense run --source sim --audio none --host 127.0.0.1 --port 8080 --no-browser
```

Visit http://localhost:8080/parents. Sign in or create the owner account with the code printed by the device. See [login and caregiver accounts](authentication.md). In simulation with `--audio none`, use **Listen in browser** to hear the cues.

## Supervised practice

Training is stationary and adult-supervised. It temporarily replaces live obstacle cues with a virtual sound. The device, rather than the browser, owns the session and stores confirmed results and observations in `training_sessions` in the account database.

- A session has one supervising connection and caregiver. Another connection cannot play or change its targets.
- The browser sends supervision heartbeats every two seconds. Losing that connection/authentication pauses immediately; missing heartbeats pause within ten seconds (plus the watchdog's 0.5-second scheduling interval). Hiding the browser tab also requests a pause.
- Pausing stops the virtual sound and returns the device to its usual obstacle-cue mode. Sensor health and power settings still apply. Practice resumes only after **Resume practice** is explicitly selected.
- Each trial and response has a stable ID. A retried response is deduplicated. **Finish & save** waits for any pending answer acknowledgement, then uses the device's authoritative results.
- Answers and notes survive a device restart. Active time is checkpointed on commands and heartbeats; abrupt power loss can omit time since the last checkpoint. A restarted session is paused and requires explicit resume.
- A storage failure pauses training rather than acknowledging an unpersisted result. After storage recovers, retry the same response.
- The owner can resume or finish an abandoned session. A caregiver can resume their own session on a new connection. Finish an existing paused session before starting another.

Goals can be linked when practice starts. The progress panel reports mean direction error and response time by sound type, practice days over the last week, a practice streak, goal-linked session counts, observations, and linked footage. CSV export includes the underlying responses and observations. These are exercise measurements, not a clinical or safe-mobility score.

## Child fitting

**Child fitting** is available to the owner and device managers. The guided flow accepts measured top-of-head and ear heights, lens height, and mounting tilt. It previews floor/head/corridor blind zones before saving. A clear floor can be used to calibrate lens height and tilt. Calibration is rejected if practice starts while the floor fit is being computed.

Saved measurements update sensing height limits, the head-level classification threshold, spatial audio ear height, sensor mount geometry, and coverage calculations; they persist with device settings. Changing the mount through the advanced console clears its checked flag. Recheck fitting as the child grows. Review the mount and exercises with an orientation and mobility specialist. The preview describes geometry, not a guarantee of detecting all objects.

## Footage and recovery

- Record depth or supported color footage for up to two minutes, without audio, or take PNG snapshots.
- Kinect v2's bridge currently streams depth only. Kinect v1 and the simulator support color.
- Link footage to a practice session at capture time or edit the association in the library.
- Clips have editable titles and notes, search, playback, downloads, and bounded media rendering.
- Loss of the selected camera stream or connection stops recording and attempts to save the footage.
- Recording can start without IndexedDB. If storage fails, a persistent recovery area offers the footage and its notes as separate downloads. Keep the page open until the downloads are saved; acknowledge the recovery copy afterward.

Footage, profile details, goals, and cached history live in IndexedDB in the current browser origin. Legacy clip storage is upgraded in place and legacy localStorage profile/history is migrated. Small draft recovery records also use localStorage when available. Storage is scoped to browser profile, hostname, and port. Clearing browser data can erase these records.

## Backup and restore

**Profile & privacy → Back up clips & notes** creates a standard, uncompressed ZIP with `manifest.json` and media files. It includes the profile, goals, practice history, clip titles/notes, and session associations. Unsaved media recovery copies and unsaved clip annotations are included when possible. Select clips in the library for smaller backups; with none selected, all available clips are included.

Restore validates the entire archive, schema, entry paths, and media checksums before writing. New clips and local state are written in one IndexedDB transaction. Existing clip, session, and goal IDs are retained instead of overwritten. An explicit checkbox controls replacing the local child profile. Imported unfinished sessions are history only; they cannot start a virtual sound or impersonate a live device session. Live device history takes precedence for matching session IDs.

The same limits are enforced for export and import: 256 MB per archive, 1,000 clips, an 8 MB manifest, 10,000 sessions, and 1,000 goals. Split large clip libraries into selected backups. A history exceeding the manifest limit can be downloaded separately with **Download session history**. Finish active practice and recordings before restoring.

Storage usage and persistence controls are shown in settings when supported by the browser. Persistent storage is not a backup and cannot prevent deliberate clearing of browser data.

## Privacy lock and caregiver access

An expired or revoked session immediately hides and makes private content inert, stops live playback, and preserves drafts/recordings in the background. Reauthenticate in the lock screen to recover without navigating away. When storage is unavailable, unsaved footage remains only in this open page's memory: the lock explicitly asks the user to keep it open.

Owner, viewer, trainer, and device-manager permissions are enforced by the server for APIs and WebSocket commands. Invitations and revocation are managed in **Profile & privacy → Their trusted circle**. Login protects app access; it does not encrypt local browser records against someone with operating-system or browser-profile access. Use a trusted browser profile and HTTPS for credentials and footage sent across a network.

## Verification

```sh
.venv/bin/python -m pytest -q
node --test tests/test_parent_data.mjs
node tests/test_parent_ui.cjs
```

Automated checks cover training ownership/leases/restart, response deduplication and lost acknowledgements, storage-failure recovery, calibration races, caregiver migration/permissions/revocation, ZIP validation and round-trips, duplicate restore, progress calculations, in-flight Finish, and privacy/media recovery.

Isolated browser checks used simulated data and disposable test accounts: goal-linked practice with an observation, saved video playback, measured fitting preview/save, backup export, duplicate restore, fresh-origin restore with playable footage/notes/association, auth revocation mid-practice/recording, in-place reauthentication and explicit resume, viewer restrictions, and desktop/mobile layout. Physical Kinect fitting, walking performance, and clinical validation still require hands-on evaluation.
