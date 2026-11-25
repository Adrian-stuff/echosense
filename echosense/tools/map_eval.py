"""World map / scan-matching benchmark: ``echosense eval-map``.

Walks each simulator scene's route (auto-walk, with chest bob) and compares the pose the world
map estimates from depth alone with the simulator's true sensor pose. Reports drift (position
error at the end as a share of the distance walked), worst error, heading error, the share of
frames where matching was weak (dead reckoning) and the time the map adds per frame.
"""
from __future__ import annotations

import math
import statistics

import numpy as np

from ..config import load_config
from ..processing.pipeline import DepthProcessor
from ..sensors.base import DepthFrame
from ..sensors.simulated import SimulatedKinect

SCENES = ["maysan_street", "obstacle_course", "open_area"]


def walk(cfg, scene: str, seconds: float = 40.0, seed: int = 1, bob: bool = True, mapper_cfg: dict | None = None):
    if mapper_cfg is not None:
        cfg = {**cfg, "processing": {**cfg["processing"], "world_map": {**cfg["processing"].get("world_map", {}), **mapper_cfg}}}
    sim = SimulatedKinect(cfg, scene, seed=seed)
    sim.autowalk, sim.head_bob = True, bob
    proc = DepthProcessor(cfg)
    fps = sim.fps
    errs, yerrs, weak, lost, ms = [], [], 0, 0, []
    true_walked, last = 0.0, None
    n = int(seconds * fps)
    f = 0
    for f in range(n):
        t = f / fps
        if sim.route and sim.route_i >= len(sim.route):
            break  # end of the route (the simulator would teleport back to the start)
        sim._step(1.0 / fps, t)
        depth, extra = sim.capture(t)
        res = proc.process(DepthFrame(depth, t, f + 1, sim.intrinsics, "sim", extra), 4.5)
        w = res.world
        ms.append(res.timings["map"])
        p = (extra["pose"]["x"], extra["pose"]["z"])
        if last is not None:
            true_walked += math.hypot(p[0] - last[0], p[1] - last[1])
        last = p
        if f > 0:
            weak += w["status"] != "mapping"
            lost += w["status"] == "lost"
            errs.append(w["err_pos"])
            yerrs.append(w["err_yaw"])
    m = proc.mapper
    frames = max(1, len(errs))
    return {"scene": scene, "frames": frames + 1, "walked": true_walked, "est_walked": m.travelled,
            "lost": 100.0 * lost / frames, "end_err": errs[-1] if errs else 0.0,
            "max_err": max(errs) if errs else 0.0, "yaw_end": yerrs[-1] if yerrs else 0.0,
            "yaw_max": max(yerrs) if yerrs else 0.0, "weak": 100.0 * weak / frames,
            "drift": 100.0 * (errs[-1] if errs else 0.0) / max(true_walked, 1e-6),
            "ms_mean": statistics.mean(ms), "ms_p95": float(np.percentile(ms, 95))}


def run(args):
    cfg = load_config(args.config, args.profile)
    if args.preset:
        cfg.setdefault("sim", {})["sensor_preset"] = args.preset
    scenes = [args.scene] if args.scene else SCENES
    print(f"\nWorld map: estimated pose vs simulator truth  (sensor {cfg.get('sim', {}).get('sensor_preset', 'kinect_v2')}, "
          f"profile {cfg['device_profile']}, up to {args.seconds:.0f} s per route, seeds {args.seeds})\n")
    rows = []
    for sc in scenes:
        for seed in range(args.seeds):
            r = walk(cfg, sc, args.seconds, seed=seed + 1, bob=not args.no_bob)
            rows.append(r)
            print(f"  {sc:<16} seed {seed + 1}  walked {r['walked']:5.1f} m  end error {100 * r['end_err']:5.1f} cm "
                  f"({r['drift']:5.1f}% drift)  worst {100 * r['max_err']:5.1f} cm  heading end {r['yaw_end']:4.1f} deg "
                  f"worst {r['yaw_max']:4.1f} deg  weak {r['weak']:4.1f}% (lost {r['lost']:4.1f}%)  map {r['ms_mean']:4.1f} ms (p95 {r['ms_p95']:4.1f})")
    blind = [r["scene"] for r in rows if r["lost"] > 80]
    if blind:
        print(f"\n  {', '.join(sorted(set(blind)))}: nothing to match in view most of the time (open space). Depth alone "
              f"can't follow the walk there; a gyro / IMU would have to.")
    rows = [r for r in rows if r["lost"] <= 80]
    if rows:
        print(f"\n  mean drift {statistics.mean(r['drift'] for r in rows):.1f}%   "
              f"mean heading error at end {statistics.mean(r['yaw_end'] for r in rows):.1f} deg   "
              f"map time {statistics.mean(r['ms_mean'] for r in rows):.1f} ms/frame\n")
    return rows
