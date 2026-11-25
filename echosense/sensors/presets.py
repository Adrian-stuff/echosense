"""Depth-sensor models for the simulator, to compare field of view and range before buying hardware.

The Kinect v2's field of view (about 70 x 60 deg) is fixed by its optics. Wider coverage needs a
different sensor (or two). These presets let the simulator, the pipeline and the coverage
figures run with each candidate. Figures come from the manufacturers' datasheets. Wide-angle
lenses are modelled as pinhole (no fisheye distortion), so treat edge-of-view results as approximate.
Only the Kinect v2 has a hardware driver in this project.
"""
from __future__ import annotations

import math


def _pinhole(w, h, hfov, vfov):
    fx = (w / 2) / math.tan(math.radians(hfov / 2))
    fy = (h / 2) / math.tan(math.radians(vfov / 2))
    return {"width": w, "height": h, "fx": round(fx, 3), "fy": round(fy, 3), "cx": (w - 1) / 2, "cy": (h - 1) / 2}


PRESETS = {
    "kinect_v2": {"label": "Kinect v2 (70x60°, 0.5-4.5 m)", "model": "kinect_v2",
                  "width": 512, "height": 424, "fx": 365.456, "fy": 365.456, "cx": 254.878, "cy": 205.395,
                  "min_depth": 0.5, "max_depth": 4.5, "fps": 30, "noise": [0.0015, 0.0008]},
    "kinect_v1": {"label": "Kinect v1 / Xbox 360 (57x45°, 0.8-4 m, USB 2.0)", "model": "kinect_v1",
                  "width": 640, "height": 480, "fx": 585.0, "fy": 585.0, "cx": 319.5, "cy": 239.5,
                  "min_depth": 0.8, "max_depth": 4.0, "fps": 30, "noise": [0.001, 0.0014],
                  "noise_model": "structured_light"},
    "azure_nfov": {"label": "Azure Kinect / Orbbec Femto, narrow (75x65°, 0.5-3.9 m)", "model": "azure_nfov",
                   **_pinhole(640, 576, 75, 65), "min_depth": 0.5, "max_depth": 3.86, "fps": 30, "noise": [0.0012, 0.0007]},
    "azure_wfov": {"label": "Azure Kinect / Orbbec Femto, wide (120x120°, 0.25-2.9 m)", "model": "azure_wfov",
                   **_pinhole(512, 512, 120, 120), "min_depth": 0.25, "max_depth": 2.88, "fps": 30, "noise": [0.002, 0.0015]},
    "realsense_d455": {"label": "Intel RealSense D455 stereo (87x58°, 0.6-6 m)", "model": "realsense_d455",
                       **_pinhole(848, 480, 87, 58), "min_depth": 0.6, "max_depth": 6.0, "fps": 30, "noise": [0.002, 0.0025]},
}

SPEC_KEYS = ("model", "label", "width", "height", "fx", "fy", "cx", "cy", "min_depth", "max_depth", "fps")


def fov_deg(spec: dict) -> tuple[float, float]:
    return (math.degrees(2 * math.atan2(spec["width"] / 2, spec["fx"])),
            math.degrees(2 * math.atan2(spec["height"] / 2, spec["fy"])))


def coverage(spec: dict, mount_height: float, pitch_deg: float, head_height: float = 1.7,
             corridor_half: float = 0.45) -> dict:
    """Where this sensor, mounted like this, can see: the blind zones the user must know about."""
    hfov, vfov = fov_deg(spec)
    lo = math.radians(pitch_deg - vfov / 2)  # lowest ray, negative = below horizontal
    hi = math.radians(pitch_deg + vfov / 2)
    floor = mount_height / math.tan(-lo) if lo < 0 else None
    head = (head_height - mount_height) / math.tan(hi) if hi > 0 and head_height > mount_height else (0.0 if head_height <= mount_height else None)
    corridor = corridor_half / math.tan(math.radians(hfov / 2))
    rng = spec["max_depth"]

    def clip(d):
        if d is None:
            return None
        d = max(d, spec["min_depth"])
        return round(d, 2) if d <= rng else None

    return {"hfov": round(hfov, 1), "vfov": round(vfov, 1), "floor_from": clip(floor), "head_from": clip(head),
            "corridor_from": clip(corridor), "width_at_2m": round(2 * 2 * math.tan(math.radians(hfov / 2)), 2),
            "max_depth": rng, "min_depth": spec["min_depth"], "head_height": head_height}
