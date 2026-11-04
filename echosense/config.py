"""Configuration for Echo-Sense.

Configuration is plain JSON-compatible dicts so it can be shipped to the web
viewer, stored next to session logs, and overridden per device profile
(``echosense/profiles/*.json``) or per file (``--config my.json``).

User *settings* (volume, range, mode ...) are different: they belong to the
person wearing the device and are persisted by ``device.settings``.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent
SCENES_DIR = PACKAGE_DIR / "sim" / "scenes"
PROFILES_DIR = PACKAGE_DIR / "profiles"
WEB_DIR = PACKAGE_DIR / "web"

DEFAULTS: dict = {
    "device_profile": "dev",
    "sensor": {
        # Microsoft Kinect v2 depth (IR) camera, figures from the proposal
        # (Kurillo et al., 2022) and libfreenect2 factory intrinsics.
        "model": "kinect_v2",
        "width": 512,
        "height": 424,
        "fx": 365.456,
        "fy": 365.456,
        "cx": 254.878,
        "cy": 205.395,
        "min_depth": 0.5,
        "max_depth": 4.5,
        "fps": 30,
        # Chest mount. Pitch is negative when the sensor is tilted down.
        "mount_height": 1.25,
        "mount_pitch_deg": -10.0,
        # libfreenect2 delivers depth mirrored left/right; verify on hardware
        # with the hand test described in the README.
        "flip_horizontal": True,
        "kinect2_pipeline": "auto",  # auto | cl | gl | cpu
        "kinect2_bridge": "",  # path to echosense-kinect2, empty = search
    },
    "processing": {
        "stride": 3,  # pixel subsampling before 3D projection
        "downsample": "near_median",  # near_median (keeps thin objects) | stride
        "near_layer": 0.25,
        "hold_frames": 2,  # keep a flickering pixel's last depth this many frames
        "flying_pixel_filter": True,
        "column_estimator": True,
        "ground_tolerance": 0.08,  # |height| below this is ground (m)
        "drop_threshold": 0.12,  # below the ground plane by this = drop-off
        "user_height": 1.75,
        "head_margin": 0.10,
        "corridor_half_width": 0.45,  # walking corridor, each side of centre
        "body_half_width": 0.25,
        "cell_size": 0.10,
        "grid_half_width": 4.0,
        "grid_depth": 6.5,
        "min_cell_points": 3,
        "min_cluster_points": 6,
        "ransac_iterations": 48,
        "ransac_threshold": 0.04,
        "ground_search_band": 0.30,
        "ground_smoothing": 0.35,
        "confirm_frames": 3,  # false-positive suppression
        "drop_time": 0.35,  # forget a track unseen for this long (s)
        "assoc_gate": 0.6,
        "smoothing": 0.5,
        "velocity_gain": 0.15,
        "overhang_min_height": 1.0,  # nothing below this = head-level hazard
        "low_max_height": 0.45,  # nothing above this = low / tripping hazard
        "priority_bonus": 0.25,  # head-level and drop-offs reported earlier
        "report_off_path": False,
    },
    "ranges": {"near": 1.5, "medium": 2.5, "far": 4.0},
    "sim": {"sensor_preset": "kinect_v2"},  # see sensors/presets.py: compare wider-FOV sensors
    "audio": {
        "sample_rate": 48000,
        "block_size": 1024,
        "driver": "local",  # local (speakers/headphones) | none
        "max_cues": 2,
        "max_output_dbfs": -6.0,  # output level limit (ear safety)
        "ear_height": 1.60,
        "hrtf": "structural",  # Brown & Duda spherical-head + pinna model
        "head_radius": 0.0875,
    },
    "speech": {"engine": "auto", "rate": 175, "voice": ""},
    "health": {
        "no_data_timeout": 1.0,
        "blocked_valid_ratio": 0.10,
        "blocked_time": 1.0,
        "degraded_quality": 0.75,  # below this share of floor returns, sensing is degraded
        "degraded_time": 1.5,
        "alert_repeat": 8.0,
    },
    "battery": {"capacity_hours": 4.0, "low_warning": 20, "critical_warning": 10},
    "web": {"enabled": True, "host": "127.0.0.1", "port": 8080, "viewer_stride": 2, "viewer_fps": 15},
    "paths": {"settings": "~/.echosense/settings.json", "logs": "logs"},
    "phrases": {
        "startup": "Echo-Sense starting.",
        "ready": "Ready. {battery} percent battery. Range {range}. {mode} mode.",
        "power_off": "Echo-Sense off.",
        "volume": "Volume {volume}.",
        "range": "Range {range}, {meters} meters.",
        "mode_continuous": "Continuous mode.",
        "mode_warning": "Warning mode.",
        "battery": "Battery {battery} percent.",
        "battery_low": "Battery low. {battery} percent.",
        "battery_critical": "Battery critical. {battery} percent. Use your cane.",
        "sensor_ok": "Sensor OK.",
        "sensor_blocked": "Sensor blocked. Use your cane.",
        "sensor_no_data": "Sensor not responding. Use your cane.",
        "sensor_degraded": "Depth sensing degraded. Take extra care.",
        "status": "{sensor}. Range {range}. {mode} mode. {battery} percent battery.",
        "training_on": "Training mode.",
        "training_off": "Training mode off.",
        "profile": "Sound profile {profile}.",
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def list_profiles() -> list[str]:
    return sorted(p.stem for p in PROFILES_DIR.glob("*.json"))


def load_config(path: str | None = None, profile: str | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    profile = profile or os.environ.get("ECHOSENSE_PROFILE")
    if profile:
        ppath = PROFILES_DIR / f"{profile}.json"
        if not ppath.exists():
            raise SystemExit(f"Unknown profile '{profile}'. Available: {', '.join(list_profiles())}")
        cfg = deep_merge(cfg, json.loads(ppath.read_text()))
        cfg["device_profile"] = profile
    if path:
        cfg = deep_merge(cfg, json.loads(Path(path).read_text()))
    return cfg


def expand(path: str) -> Path:
    p = Path(os.path.expanduser(path))
    return p if p.is_absolute() else Path.cwd() / p
