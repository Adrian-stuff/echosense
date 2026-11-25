"""Hard-case depth-sensing benchmark: ``echosense eval-sensing``.

The lab protocol uses one flat panel. Real streets are harder: thin poles,
hanging cables, foliage and glass, sunlight, low curbs, drop-offs and people
walking towards the user. Each case here runs several noise seeds through the
pipeline and reports detection rate, distance error, frame-to-frame distance
jitter (which the user hears as an unsteady ping rate) and, for moving
obstacles, how far the reported distance lags the truth.
"""
from __future__ import annotations

import math
import statistics

import numpy as np

from ..config import load_config
from ..processing.pipeline import DepthProcessor
from ..sensors.base import DepthFrame
from ..sensors.simulated import SimulatedKinect

S_OFF = 0.12  # sensor ahead of the body centre


def _scene(*objs, ground="concrete"):
    return {"id": "eval", "name": "eval", "ground": ground, "start": {"x": 0, "z": 0, "yaw": 0}, "objects": list(objs)}


def _panel(d, material, w=0.5, h=1.2, y0=0.2):
    return {"type": "box", "name": "panel", "x": 0, "z": S_OFF + d + 0.02, "w": w, "h": h, "d": 0.04, "y0": y0, "material": material}


CASES = [
    # name, scene, true distance (m), expected kind, sunlight
    ("thin pole 5 cm @ 1.5 m", _scene({"type": "cylinder", "name": "pole", "x": 0.05, "z": S_OFF + 1.5 + 0.025, "r": 0.025, "y1": 2.2, "material": "metal"}), 1.5, None, 0.0),
    ("thin pole 5 cm @ 3.0 m", _scene({"type": "cylinder", "name": "pole", "x": 0.05, "z": S_OFF + 3.0 + 0.025, "r": 0.025, "y1": 2.2, "material": "metal"}), 3.0, None, 0.0),
    ("thin pole 5 cm @ 4.0 m", _scene({"type": "cylinder", "name": "pole", "x": 0.05, "z": S_OFF + 4.0 + 0.025, "r": 0.025, "y1": 2.2, "material": "metal"}), 4.0, None, 0.0),
    ("hanging cable 3 cm @ 2.0 m", _scene({"type": "box", "name": "cable", "x": 0, "z": S_OFF + 2.0 + 0.015, "w": 1.6, "h": 0.03, "d": 0.03, "y0": 1.70, "material": "wood"}), 2.0, "head", 0.0),
    ("hanging cable 3 cm @ 3.0 m", _scene({"type": "box", "name": "cable", "x": 0, "z": S_OFF + 3.0 + 0.015, "w": 1.6, "h": 0.03, "d": 0.03, "y0": 1.70, "material": "wood"}), 3.0, "head", 0.0),
    ("low sign @ 2.5 m (head level)", _scene({"type": "box", "name": "sign", "x": 0, "z": S_OFF + 2.5 + 0.02, "w": 0.8, "h": 0.3, "d": 0.04, "y0": 1.55, "material": "metal"}), 2.5, "head", 0.0),
    ("curb 10 cm @ 2.0 m", _scene({"type": "box", "name": "curb", "x": 0, "z": S_OFF + 2.0 + 0.2, "w": 1.5, "h": 0.10, "d": 0.4, "material": "concrete"}), 2.0, "low", 0.0),
    ("step down 15 cm @ 2.0 m", _scene({"type": "pit", "name": "step", "x0": -1.0, "x1": 1.0, "z0": S_OFF + 2.0, "z1": S_OFF + 6.0, "depth": 0.15}), 2.0, "drop", 0.0),
    ("foliage, direct sun @ 3.0 m", _scene(_panel(3.0, "foliage")), 3.0, None, 0.9),
    ("foliage, direct sun @ 4.0 m", _scene(_panel(4.0, "foliage")), 4.0, None, 0.9),
    ("concrete, direct sun @ 4.0 m", _scene(_panel(4.0, "concrete")), 4.0, None, 0.9),
    ("dark fabric @ 3.5 m", _scene(_panel(3.5, "fabric")), 3.5, None, 0.3),
    ("glass panel @ 2.0 m", _scene(_panel(2.0, "glass")), 2.0, None, 0.0),
]


def _run_static(cfg, sc, d, kind, sun, seeds, frames):
    det, errs, jit, kinds = [], [], [], []
    cf = DepthProcessor(cfg).frames_to_confirm(30)  # includes the sensitivity level's extra frames
    for seed in range(seeds):
        sim = SimulatedKinect(cfg, sc, seed=100 + seed)
        sim.sun, sim.head_bob = sun, False
        proc = DepthProcessor(cfg)
        rs = []
        hit_frames = 0
        for f in range(frames):
            depth, _ = sim.capture(f / 30)
            res = proc.process(DepthFrame(depth, f / 30, f + 1, sim.intrinsics, "sim"), 4.5, True)
            cand = [o for o in res.reported if abs(o["r"] - d) < 0.6 and (kind is None or o["kind"] == kind or kind == "low" and o["kind"] == "body")]
            if f >= cf + 1:
                hit_frames += bool(cand)
                if cand:
                    best = min(cand, key=lambda o: abs(o["r"] - d))
                    rs.append(best["r"])
                    kinds.append(best["kind"])
        n_eval = frames - cf - 1
        det.append(hit_frames / n_eval)
        errs += [abs(r - d) for r in rs]
        if len(rs) > 3:
            jit.append(float(np.std(np.diff(rs))))
    return {"detect": 100 * statistics.mean(det), "mae_cm": 100 * statistics.mean(errs) if errs else None,
            "jitter_cm": 100 * statistics.mean(jit) if jit else None,
            "kind": max(set(kinds), key=kinds.count) if kinds else "-"}


def _run_pedestrian(cfg, seeds):
    """Person walking straight at the user at 1.3 m/s from 4.5 m to 0.8 m."""
    lags, det = [], []
    speed = 1.3
    for seed in range(seeds):
        sc = _scene({"type": "pedestrian", "name": "walker", "path": [[0.0, S_OFF + 4.7], [0.0, S_OFF + 0.8]], "speed": speed})
        sim = SimulatedKinect(cfg, sc, seed=200 + seed)
        sim.head_bob = False
        proc = DepthProcessor(cfg)
        hits = 0
        n = 0
        for f in range(int(3.2 * 30)):
            t = f / 30
            depth, _ = sim.capture(t)
            res = proc.process(DepthFrame(depth, t, f + 1, sim.intrinsics, "sim"), 4.5, True)
            m = sim.world.movers[0]
            true_r = (m.z - S_OFF) - 0.22  # front of the body
            if 1.0 < true_r < 3.8:
                n += 1
                c = [o for o in res.reported if abs(o["az"]) < 15]
                if c:
                    hits += 1
                    lags.append(min(o["r"] for o in c) - true_r)
        det.append(hits / max(1, n))
    return {"detect": 100 * statistics.mean(det), "mean_lag_cm": 100 * statistics.mean(lags) if lags else None,
            "max_over_cm": 100 * max(lags) if lags else None}


def evaluate(cfg, seeds=4, frames=24, quiet=False):
    rows = []
    for name, sc, d, kind, sun in CASES:
        r = _run_static(cfg, sc, d, kind, sun, seeds, frames)
        rows.append((name, r))
        if not quiet:
            print(f"  {name:<32} detect {r['detect']:5.1f}%  error {_f(r['mae_cm'])} cm  jitter {_f(r['jitter_cm'])} cm  kind {r['kind']}")
    ped = _run_pedestrian(cfg, seeds)
    if not quiet:
        print(f"  {'pedestrian approaching 1.3 m/s':<32} detect {ped['detect']:5.1f}%  reported minus true: mean {_f(ped['mean_lag_cm'])} cm, "
              f"worst {_f(ped['max_over_cm'])} cm (positive = says farther than it is)")
    return rows, ped


def _f(v):
    return "  —  " if v is None else f"{v:5.1f}"


def run(args):
    cfg = load_config(args.config, args.profile)
    if args.preset:
        cfg.setdefault("sim", {})["sensor_preset"] = args.preset
    print(f"\nDepth sensing hard cases  (sensor {cfg.get('sim', {}).get('sensor_preset', 'kinect_v2')}, "
          f"profile {cfg['device_profile']}, stride {cfg['processing']['stride']}, "
          f"{args.seeds} seeds x {args.frames} frames)\n")
    rows, ped = evaluate(cfg, args.seeds, args.frames)
    det = statistics.mean(r["detect"] for _, r in rows)
    maes = [r["mae_cm"] for _, r in rows if r["mae_cm"] is not None]
    jit = [r["jitter_cm"] for _, r in rows if r["jitter_cm"] is not None]
    print(f"\n  mean detection {det:.1f}%   mean error {statistics.mean(maes):.1f} cm   mean jitter {statistics.mean(jit):.1f} cm\n")
