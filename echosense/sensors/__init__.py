"""Depth sources. ``open_source`` picks one by name; ``auto`` prefers real hardware."""
from __future__ import annotations

from .base import DepthFrame, DepthSource, SourceError
from .kinect2 import Kinect2Source, find_bridge
from .replay import Recorder, ReplaySource
from .simulated import SimulatedKinect

__all__ = ["DepthFrame", "DepthSource", "SourceError", "open_source", "available_sources", "Recorder",
           "SimulatedKinect", "Kinect2Source", "ReplaySource"]


def available_sources(cfg: dict) -> list[dict]:
    out = [{"id": "sim", "label": "Simulator", "available": True}]
    bridge = find_bridge(cfg["sensor"].get("kinect2_bridge", ""))
    out.append({"id": "kinect2", "label": "Kinect v2", "available": bool(bridge),
                "note": "" if bridge else "bridge not built"})
    try:
        import freenect  # noqa: F401  type: ignore
        k1 = True
    except ImportError:
        k1 = False
    out.append({"id": "kinect1", "label": "Kinect v1", "available": k1, "note": "" if k1 else "freenect not installed"})
    return out


def open_source(name: str, cfg: dict, scene: str = "maysan_street") -> DepthSource:
    """Create *and start* a depth source. Raises SourceError if it can't be opened."""
    if name == "auto":
        errors = []
        for cand in ("kinect2", "kinect1"):
            try:
                return open_source(cand, cfg, scene)
            except SourceError as e:
                errors.append(f"{cand}: {e}")
        src = open_source("sim", cfg, scene)
        src.error = "No Kinect found, using simulator. " + " | ".join(errors)
        return src
    if name == "sim":
        src = SimulatedKinect(cfg, scene)
    elif name == "kinect2":
        src = Kinect2Source(cfg)
    elif name == "kinect2-fake":
        src = Kinect2Source(cfg, fake=True)
    elif name == "kinect1":
        from .kinect1 import Kinect1Source

        src = Kinect1Source(cfg)
    elif name.startswith("replay:"):
        src = ReplaySource(name.split(":", 1)[1])
    else:
        raise SourceError(f"unknown source {name!r}")
    src.start()
    return src
