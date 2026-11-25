# Parent improvements implementation checklist

The active goal includes every item below. Checked items require implementation and verification, not just a design note.

- [x] Device-managed training: ownership, supervision timeout, pause on lost connection/authentication, explicit resume, preserved obstacle-cue mode.
- [x] Child fitting: measured head/ear height, mount calibration workflow, preview coverage, persisted settings used by processing/audio/coverage.
- [x] Reliable results: trial and response IDs, acknowledgement/retry/deduplication, finish reconciles authoritative results.
- [x] Recording recovery: record without IndexedDB; persistent direct-download recovery for unsaved media.
- [x] Backup/restore: footage, metadata, session associations, profiles/goals; validation and duplicate handling; storage usage/persistence controls.
- [x] Privacy lock: hide private content on expiry/revocation, save drafts and stop recordings first/in background, require reauthentication to unlock.
- [x] Progress: sound-type errors, practice consistency, observations, goals, session-linked clips, downloadable report.
- [x] Caregiver accounts: invitation acceptance, separate logins, enforced view/train/configure permissions, owner revocation.

Validation must include backend integration tests, meaningful frontend data/state tests, desktop/mobile browser checks, and a final scope audit. Real Kinect hardware and clinical validation are outside software verification; their limits must remain explicit.

## Evidence checked

- Supervision, ownership, restart checkpoints, pending-response deduplication, lost acknowledgement, and disk-failure retry: `tests/test_supervision.py`.
- Child geometry/processing/audio persistence and calibration race: `tests/test_supervision.py` and `tests/test_caregivers.py`; browser preview/save at head110cm, ear100cm, lens80cm.
- Caregiver migration, invitation acceptance/reuse, role restrictions, and revocation: `tests/test_caregivers.py`; browser viewer controls disabled and owner settings present.
- Original login/session protections: `tests/test_auth.py`.
- Full Python regression suite: 110 passed. Latest affected backend checks: 15 passed.
- Actual frontend handlers: `node tests/test_parent_ui.cjs` passed in-flight Finish, no-IDB recording, persistent recovery copy, synchronous privacy lock, and rebuilt playback URLs after unlock.
- Backup/report/privacy modules: `node --test tests/test_parent_data.mjs` passed nine tests covering complete round-trips, corruption, duplicate merge, export/import limits, imported-history isolation, report grouping and links, and storage failure.
- Isolated browser: goal-linked practice/result/observation; live recording/playback; measured fitting preview/save; backup export; duplicate-safe restore; fresh-origin restore with playable video and correct note/session link; auth revocation during practice/recording; hidden private content, retained draft and saved recording; in-place unlock and explicit resume; viewer-only controls; desktop and390px mobile layout without horizontal document overflow.
- A separate static review's active-duration, calibration-race, and archive-limit findings were corrected and scored resolved.
- User-facing guides were reconciled with the shipped behavior. No actual child footage or user account credentials were used in QA. Temporary simulator QA servers/tabs were closed.

## Verification limits

The fitting implementation uses the actual processing, audio, sensor geometry, and calibration code paths, tested with simulated data and controlled floor-fit fixtures. Physical sensor fit, battery fuel-gauge hardware, walking performance, and clinical effectiveness require hands-on evaluation. They are not represented as verified by these software checks.
