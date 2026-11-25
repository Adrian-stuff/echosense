# EchoSense

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Parents and caregivers of visually impaired children use the parent dashboard to review training and capture footage. Device developers and caregivers retain the existing console for fitting, simulation, and advanced controls.

## Product Purpose

EchoSense turns Kinect depth sensing into spatial audio obstacle cues. The parent companion supports supervised training, observation, and reflection on progress.

## Capabilities and Constraints

Confirmed by the user: add the dashboard alongside the console; record the available camera or depth view with training notes.

Existing software provides a local aiohttp server, WebSocket depth and audio streaming, optional color frames, device health, and stationary direction training. Kinect v2's bridge currently streams depth only. Kinect v1 supports color. Battery readings are simulated until fuel-gauge hardware is connected.

The parent app stores clips, snapshots, profiles, goals, and cached history in IndexedDB, with small draft recovery records in localStorage. Confirmed supervised practice and observations are also persisted on the device. Portable ZIP backups include footage and its linked records. The device owner and invited caregivers have separate view, training, and device-configuration permissions enforced on the dashboard APIs and live stream. Owner setup requires a device-terminal code; caregivers accept one-time invitations. Passwords are hashed and sessions expire or can be revoked. Expiry locks private content and preserves work for in-place reauthentication. Training has a single supervising connection, a heartbeat lease, explicit pause/resume, and idempotent acknowledged results. Measured child fitting updates processing, audio, and coverage. There are no cloud accounts, GPS tracking, remote caregiver pairing, or cloud backups. These are explicit implementation limits, not decisions about a future release.

## Accessibility & Inclusion

The device serves visually impaired users. Parent controls must have readable labels, keyboard access, visible focus, and screen-reader status messages. Training is adult-supervised and stationary; exercise error is not a mobility assessment.

## Evidence on Hand

See README.md, echosense/device/training.py, echosense/server.py, and the sensor drivers. Hardware integration and clinical validation of child-specific training remain unverified by this change.

## Product Principles

- Support the child's independence and choice.
- Label simulated data and distinguish depth from color footage.
- Record only through an explicit action.
- Preserve existing assistive cues and advanced console workflows.
