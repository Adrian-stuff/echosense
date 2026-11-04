"""``echosense train-objects``: train the depth-only object recogniser on simulator scenes.

1. Generate random scenes (people standing or walking, poles and trunks, cars / tricycles /
   motorcycles, walls and gates, stalls / boxes / bins, curbs / benches, signs / cables /
   awnings / tree canopies) with random sizes, materials, sunlight, mount height and tilt,
   seen by Kinect v2 and Kinect v1 models at the strides the device profiles use.
2. Run the real pipeline on each scene. Every obstacle it reports is labelled with the
   simulator's ground truth.
3. Fit a small decision tree, test it on held-out random scenes *and* on the hand-built street
   and obstacle-course scenes (never used for training), and write the model + a report.

Add real data with ``--extra rows.csv`` (columns: the feature names + ``label``).
"""
from __future__ import annotations

import csv
import json
import math
import multiprocessing as mp
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from ..config import load_config
from ..processing.objects import CLASSES, FEATURES, MODEL_PATH, DecisionTree

S_OFF = 0.12


def _rand_object(rng, cls, x, z, i):
    U = rng.uniform
    mats = ("concrete", "metal", "wood", "fabric")
    name = f"{cls}{i}"
    if cls == "person":
        h, r = U(1.45, 1.9), U(0.16, 0.27)
        if rng.random() < 0.4:
            path, speed = [[x, z], [x, z + 0.001]], 0.0
        else:
            a = U(0, 2 * math.pi)
            path, speed = [[x - math.cos(a), z - math.sin(a)], [x + math.cos(a), z + math.sin(a)]], U(0.4, 1.4)
        return [{"type": "pedestrian", "name": name, "path": path, "speed": speed, "phase": U(0, 1), "height": h,
                 "radius": r, "class": cls}]
    if cls == "pole":
        return [{"type": "cylinder", "name": name, "x": x, "z": z, "r": U(0.025, 0.22), "y1": U(2.0, 7.0),
                 "material": rng.choice(["concrete", "metal", "wood"]), "class": cls}]
    if cls == "vehicle":
        yaw = U(0, 180)
        kind = rng.choice(["car", "tricycle", "motorcycle"])
        if kind == "car":
            w, d = U(1.6, 1.9), U(3.6, 4.6)
            return [{"type": "box", "name": name, "group": name, "x": x, "z": z, "w": w, "h": U(0.55, 0.75), "d": d,
                     "y0": U(0.18, 0.3), "yaw": yaw, "material": "metal", "class": cls},
                    {"type": "box", "name": name, "group": name, "x": x, "z": z, "w": w * 0.9, "h": U(0.45, 0.6), "d": d * 0.5,
                     "y0": U(0.85, 1.0), "yaw": yaw, "material": rng.choice(["glass", "metal"]), "class": cls}]
        if kind == "tricycle":
            return [{"type": "box", "name": name, "x": x, "z": z, "w": U(1.0, 1.4), "h": U(1.2, 1.7), "d": U(1.8, 2.5),
                     "y0": U(0.0, 0.2), "yaw": yaw, "material": rng.choice(["metal", "fabric"]), "class": cls}]
        return [{"type": "box", "name": name, "x": x, "z": z, "w": U(0.55, 0.8), "h": U(0.85, 1.15), "d": U(1.8, 2.1),
                 "y0": U(0.1, 0.2), "yaw": yaw, "material": "metal", "class": cls}]
    if cls == "wall":
        yaw = U(-35, 35) if rng.random() < 0.7 else U(35, 90)
        return [{"type": "box", "name": name, "x": x, "z": z + 0.15, "w": U(2.5, 8.0), "h": U(2.0, 3.0), "d": U(0.12, 0.3),
                 "yaw": yaw, "material": rng.choice(["concrete", "concrete", "wood", "metal"]), "class": cls}]
    if cls == "object":
        if rng.random() < 0.3:  # bin / drum / water container
            return [{"type": "cylinder", "name": name, "x": x, "z": z, "r": U(0.22, 0.4), "y1": U(0.7, 1.2),
                     "material": rng.choice(mats), "class": cls}]
        return [{"type": "box", "name": name, "x": x, "z": z, "w": U(0.3, 1.3), "h": U(0.6, 1.6), "d": U(0.3, 1.0),
                 "yaw": U(-40, 40), "material": rng.choice(list(mats) + ["glass"]), "class": cls}]
    if cls == "low":
        return [{"type": "box", "name": name, "x": x, "z": z, "w": U(0.3, 1.8), "h": U(0.07, 0.48), "d": U(0.15, 0.9),
                 "yaw": U(-40, 40), "material": rng.choice(mats), "class": cls}]
    # overhang
    if rng.random() < 0.3:
        y = U(2.0, 2.6)
        return [{"type": "sphere", "name": name, "x": x, "y": y, "z": z + 0.5, "r": U(0.6, 1.0), "material": "foliage",
                 "class": cls}]
    return [{"type": "box", "name": name, "x": x, "z": z, "w": U(0.3, 1.8), "h": U(0.02, 0.45), "d": U(0.02, 0.8),
             "y0": U(1.2, 1.75), "yaw": U(-30, 30), "material": rng.choice(["metal", "wood", "fabric"]), "class": cls}]


def random_scene(rng, n_objects):
    objs, placed = [], []
    for i in range(n_objects):
        cls = CLASSES[rng.integers(len(CLASSES))]
        for _ in range(20):
            d, az = rng.uniform(0.9, 4.0), math.radians(rng.uniform(-30, 30))
            x, z = d * math.sin(az), S_OFF + d * math.cos(az)
            if all(math.hypot(x - px, z - pz) > 1.6 for px, pz in placed):
                break
        else:
            continue
        placed.append((x, z))
        objs += _rand_object(rng, cls, x, z, i)
    return {"id": "train", "name": "train", "ground": rng.choice(["concrete", "asphalt"]),
            "start": {"x": 0, "z": 0, "yaw": 0}, "objects": objs}


def _match(world, x, z):
    best, bd = None, 0.35
    for cls, group, x0, x1, z0, z1, _y0, _y1 in world.labelled_footprints():
        d = math.hypot(max(x0 - x, 0, x - x1), max(z0 - z, 0, z - z1))
        if d < bd:
            best, bd = cls, d
    return best


def harvest(world_scene, cfg, rng, frames=8, sun=0.0, autowalk=False, seconds=None, skip_suppressed=False):
    """Run the pipeline over a scene; return (features, label) for each confirmed, labelled obstacle."""
    from ..processing.pipeline import DepthProcessor
    from ..sensors.base import DepthFrame
    from ..sensors.simulated import SimulatedKinect

    sim = SimulatedKinect(cfg, world_scene, seed=int(rng.integers(1 << 30)))
    sim.sun = sun
    sim.autowalk = autowalk
    proc = DepthProcessor(cfg)
    rows = []
    n = int(seconds * 30) if seconds else frames
    for f in range(n):
        t = f / 30
        if autowalk:
            sim._step(1 / 30, t)
        depth, extra = sim.capture(t)
        res = proc.process(DepthFrame(depth, t, f + 1, sim.intrinsics, "sim", extra), 4.5, True)
        if f < frames - 1 and not seconds:
            continue
        if seconds and f % 6:
            continue
        pose = extra["pose"]
        yaw = math.radians(pose["yaw"])
        for tr in res.tracks:
            if not tr.confirmed or tr.features is None or tr.kind in ("drop", "edge"):
                continue
            if skip_suppressed and tr.suppressed:
                continue  # parallel walls are never announced, so their label doesn't matter
            wx = pose["x"] + tr.x * math.cos(yaw) + tr.z * math.sin(yaw)
            wz = pose["z"] - tr.x * math.sin(yaw) + tr.z * math.cos(yaw)
            label = _match(sim.world, wx, wz)
            if label:
                rows.append((tr.features.tolist(), label, tr.label))
    return rows


def _job(args):
    base_cfg, seed, n_scenes, preset, stride = args
    cfg = json.loads(json.dumps(base_cfg))
    cfg["sim"]["sensor_preset"] = preset
    cfg["processing"]["stride"] = stride
    rng = np.random.default_rng(seed)
    rows = []
    for _ in range(n_scenes):
        sc = random_scene(rng, int(rng.integers(1, 4)))
        cfg["sensor"]["mount_height"] = float(rng.uniform(1.12, 1.38))
        cfg["sensor"]["mount_pitch_deg"] = float(rng.uniform(-15, -5))
        sun = float(rng.choice([0.0, 0.0, 0.2, 0.5, 0.8]))
        rows += [(f, l) for f, l, _ in harvest(sc, cfg, rng, frames=8, sun=sun)]
    return rows


def _eval_job(args):
    cfg, scene, preset, stride, seed = args
    cfg = json.loads(json.dumps(cfg))
    cfg["sim"]["sensor_preset"] = preset
    cfg["processing"]["stride"] = stride
    return harvest(scene, cfg, np.random.default_rng(seed), autowalk=True, seconds=40, skip_suppressed=True)


def _scores(y, p, k):
    cm = np.zeros((k, k), int)
    for a, b in zip(y, p):
        cm[a, b] += 1
    acc = np.trace(cm) / max(1, cm.sum())
    return acc, cm


def run(args):
    cfg = load_config(args.config, args.profile)
    jobs_per = max(1, args.scenes // 28)
    configs = [("kinect_v2", 3), ("kinect_v2", 4), ("kinect_v1", 4), ("kinect_v1", 6)]
    jobs = []
    seed = 7
    for i in range(28):
        preset, stride = configs[i % len(configs)]
        jobs.append((cfg, seed + i, jobs_per, preset, stride))
    print(f"Generating {jobs_per * 28} random scenes on {os.cpu_count()} cores (Kinect v2 + v1, strides 3/4/6) ...")
    t0 = time.time()
    rows = []
    with mp.get_context("spawn").Pool(max(1, (os.cpu_count() or 2) - 1)) as pool:
        for r in pool.imap_unordered(_job, jobs):
            rows += r
    print(f"  {len(rows)} labelled obstacles in {time.time() - t0:.0f} s")
    if args.extra:
        with open(args.extra) as f:
            for row in csv.DictReader(f):
                rows.append(([float(row[k]) for k in FEATURES], row["label"]))
        print(f"  + rows from {args.extra}")
    X = np.array([r[0] for r in rows], np.float32)
    y = np.array([CLASSES.index(r[1]) for r in rows])
    rng = np.random.default_rng(0)
    idx = rng.permutation(len(y))
    cut = int(0.8 * len(y))
    tr_i, te_i = idx[:cut], idx[cut:]
    counts = np.bincount(y[tr_i], minlength=len(CLASSES))
    w = (counts.sum() / np.maximum(counts, 1) / len(CLASSES))[y[tr_i]]  # balance classes
    tree = DecisionTree(max_depth=args.depth, min_leaf=5).fit(X[tr_i], y[tr_i], w)
    acc_te, cm_te = _scores(y[te_i], tree.predict(X[te_i]), len(CLASSES))
    out = Path(args.model or MODEL_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(tree.to_json(), separators=(",", ":")))
    size_kb = out.stat().st_size / 1024
    print(f"  held-out random scenes: accuracy {acc_te * 100:.1f}%  ({len(te_i)} obstacles)")
    print(f"  model: {out} ({len(tree.nodes)} nodes, {size_kb:.1f} KB)")

    # held-out hand-built scenes, walked along their routes, with the model just trained
    cfg["processing"]["object_model"] = str(out)
    ev_jobs = [(cfg, sc, preset, stride, 99) for sc in ("maysan_street", "obstacle_course")
               for preset, stride in (("kinect_v2", 4), ("kinect_v1", 6))]
    with mp.get_context("spawn").Pool(len(ev_jobs)) as pool:
        ev = pool.map(_eval_job, ev_jobs)
    lines_ev = []
    for (_, sc, preset, stride, _), rows_ev in zip(ev_jobs, ev):
        known = [(l, p) for _, l, p in rows_ev]
        right = sum(1 for l, p in known if l == p)
        unk = sum(1 for _, p in known if p == "unknown")
        lines_ev.append((sc, preset, stride, len(known), 100 * right / max(1, len(known)), 100 * unk / max(1, len(known))))
        print(f"  {sc} ({preset}, stride {stride}) walk-through: {100 * right / max(1, len(known)):.1f}% correct, "
              f"{100 * unk / max(1, len(known)):.1f}% said 'obstacle' (unsure), {len(known)} labelled sightings")

    # timing
    t = time.perf_counter()
    for xx in X[:2000]:
        tree.proba(xx)
    us = (time.perf_counter() - t) / min(2000, len(X)) * 1e6

    rep = [f"# Object recogniser (depth only) — {datetime.now():%Y-%m-%d %H:%M}", "",
           f"Decision tree, depth {args.depth}, {len(tree.nodes)} nodes, {size_kb:.1f} KB, {us:.0f} µs per obstacle on this machine.",
           f"Trained on {len(tr_i)} obstacles from {jobs_per * 28} random simulator scenes (Kinect v2 and v1 models).", "",
           f"**Held-out random scenes: {acc_te * 100:.1f}% accuracy** ({len(te_i)} obstacles)", "",
           "| true \\ predicted | " + " | ".join(CLASSES) + " | recall |", "|---|" + "---|" * (len(CLASSES) + 1)]
    for i, c in enumerate(CLASSES):
        rec = cm_te[i, i] / max(1, cm_te[i].sum())
        rep.append(f"| {c} | " + " | ".join(str(v) for v in cm_te[i]) + f" | {rec * 100:.0f}% |")
    rep += ["", "Hand-built scenes (never seen in training), walking the route:", "",
            "| scene | sensor | stride | sightings | correct | unsure |", "|---|---|---|---|---|---|"]
    rep += [f"| {a} | {b} | {c} | {d} | {e:.1f}% | {f:.1f}% |" for a, b, c, d, e, f in lines_ev]
    rep += ["", "Simulator shapes are cleaner than real ones: validate on labelled Kinect recordings before relying on labels."]
    rpath = Path(args.out) / f"objects-{datetime.now():%Y%m%d-%H%M%S}.md"
    rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text("\n".join(rep) + "\n")
    print(f"  report: {rpath}")
