"""The proposal's technical performance test protocol, run against the simulator.

Distances 0.5-5.0 m, azimuths 0..+/-60 deg, five materials, three lighting
conditions, N repetitions each. Produces a CSV of every trial and a Markdown
report with mean absolute distance error, detection rate, false positive rate
and estimated latency, checked against the acceptance thresholds
(MAE <= 15 cm, detection >= 90 %, latency <= 300 ms).

This rehearses the lab procedure and catches pipeline regressions; the noise
model is an approximation of a Kinect v2, so the thesis numbers must come from
the real sensor.
"""
from __future__ import annotations

import csv
import itertools
import math
import multiprocessing as mp
import os
import statistics
import time
from datetime import datetime
from pathlib import Path

from ..config import load_config

DISTANCES = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0)
AZIMUTHS = (0, -15, 15, -30, 30, -45, 45, -60, 60)
MATERIALS = ("concrete", "glass", "metal", "fabric", "foliage")
LIGHTING = {"indoor": 0.0, "overcast": 0.3, "direct sun": 0.9}
PANEL = {"w": 0.40, "h": 1.20, "d": 0.04, "y0": 0.20}


def lab_scene(d, az, material):
    s_off = 0.12  # sensor sits in front of the body centre
    objs = []
    if d is not None:
        a = math.radians(az)
        cx, cz = (d + PANEL["d"] / 2) * math.sin(a), s_off + (d + PANEL["d"] / 2) * math.cos(a)
        objs.append({"type": "box", "name": "Test panel", "x": cx, "z": cz, "w": PANEL["w"], "h": PANEL["h"],
                     "d": PANEL["d"], "y0": PANEL["y0"], "yaw": az, "material": material})
    return {"id": "lab", "name": "Lab", "ground": "concrete", "start": {"x": 0, "z": 0, "yaw": 0}, "objects": objs}


def _trial(job):
    import numpy as np

    from ..processing.pipeline import DepthProcessor
    from ..sensors.base import DepthFrame
    from ..sensors.simulated import SimulatedKinect

    cfg, d, az, material, light, sun, reps, seed = job
    period = 1.0 / cfg["sensor"]["fps"]
    frames = DepthProcessor(cfg).frames_to_confirm(cfg["sensor"]["fps"]) + 1
    scene = lab_scene(d, az, material) if d is not None else lab_scene(None, 0, material)
    sim = SimulatedKinect(cfg, scene, seed=seed)
    sim.sun = sun
    sim.head_bob = False
    proc = DepthProcessor(cfg)
    rows = []
    for rep in range(reps):
        proc.reset()
        best, ms = None, []
        for f in range(frames):
            depth, _ = sim.capture(f * period)
            t0 = time.perf_counter()
            res = proc.process(DepthFrame(depth, rep * 10 + f * period, f + 1, sim.intrinsics, "sim"), 5.0, True)
            ms.append((time.perf_counter() - t0) * 1000)
        cands = res.reported
        if d is not None:
            cands = [o for o in cands if abs(o["az"] - az) < 12 and abs(o["r"] - d) < 0.6]
            best = min(cands, key=lambda o: abs(o["r"] - d)) if cands else None
        rows.append({
            "condition": "target" if d is not None else "empty", "distance_m": d, "azimuth_deg": az, "material": material,
            "lighting": light, "rep": rep, "detected": int(best is not None) if d is not None else "",
            "measured_m": round(best["r"], 4) if best else "", "abs_error_cm": round(abs(best["r"] - d) * 100, 2) if best else "",
            "measured_az": best["az"] if best else "", "in_path": best["in_path"] if best else "",
            "false_positive": int(bool(res.reported)) if d is None else "", "process_ms": round(float(np.mean(ms)), 2),
        })
    return rows


def run(args):
    cfg = load_config(args.config, args.profile)
    reps = args.reps or (5 if args.quick else 30)
    mats = ("concrete",) if args.quick else MATERIALS
    lights = {"indoor": 0.0} if args.quick else LIGHTING
    jobs = []
    seed = 1000
    for (d, az, m, (ln, sun)) in itertools.product(DISTANCES, AZIMUTHS, mats, lights.items()):
        jobs.append((cfg, d, az, m, ln, sun, reps, seed))
        seed += 1
    for m, (ln, sun) in itertools.product(mats, lights.items()):
        jobs.append((cfg, None, 0, m, ln, sun, reps * 4, seed))
        seed += 1
    n = len(jobs)
    print(f"Running {n} conditions x {reps} repetitions on {os.cpu_count()} cores ...")
    t0 = time.time()
    rows = []
    with mp.get_context("spawn").Pool(max(1, (os.cpu_count() or 2) - 1)) as pool:
        for i, r in enumerate(pool.imap_unordered(_trial, jobs, chunksize=2), 1):
            rows.extend(r)
            if i % max(1, n // 20) == 0:
                print(f"  {i}/{n} conditions  ({time.time() - t0:.0f} s)")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = out / f"protocol-{stamp}.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    md = report(rows, cfg, reps, mats, lights)
    md_path = out / f"protocol-{stamp}.md"
    md_path.write_text(md)
    print(md.split("## Detail")[0])
    print(f"Wrote {csv_path} and {md_path}")


def report(rows, cfg, reps, mats, lights) -> str:
    tgt = [r for r in rows if r["condition"] == "target"]
    emp = [r for r in rows if r["condition"] == "empty"]
    max_d = cfg["sensor"]["max_depth"]
    half_fov = math.degrees(math.atan2(cfg["sensor"]["width"] / 2, cfg["sensor"]["fx"]))

    def stats(sel):
        det = [r["detected"] for r in sel]
        errs = [r["abs_error_cm"] for r in sel if r["detected"]]
        return (100 * sum(det) / len(det) if det else float("nan"), statistics.mean(errs) if errs else float("nan"))

    in_env = [r for r in tgt if r["distance_m"] <= 4.0 and abs(r["azimuth_deg"]) <= 30]
    det_rate, mae = stats(in_env)
    fp = 100 * sum(r["false_positive"] for r in emp) / max(1, len(emp))
    proc = statistics.mean(r["process_ms"] for r in rows)
    period = 1000 / cfg["sensor"]["fps"]
    from ..processing.pipeline import DepthProcessor
    cf = DepthProcessor(cfg).frames_to_confirm(cfg["sensor"]["fps"])
    latency = period + (cf - 1) * period + proc + 1000 * cfg["audio"]["block_size"] / cfg["audio"]["sample_rate"] + 10
    ok = lambda b: "PASS" if b else "FAIL"  # noqa: E731
    lines = [
        "# Echo-Sense technical performance protocol (simulation)", "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M}. {reps} repetitions per condition; materials: {', '.join(mats)}; "
        f"lighting: {', '.join(lights)}. Profile `{cfg['device_profile']}`, stride {cfg['processing']['stride']}.", "",
        "Simulated Kinect v2 (noise model approximates published behaviour). **Use this to rehearse the lab procedure and to "
        "catch regressions, not as thesis results.**", "",
        f"Operating envelope evaluated: 0.5-4.0 m and |azimuth| <= 30 deg (sensor: {cfg['sensor']['min_depth']}-{max_d} m, "
        f"+/-{half_fov:.0f} deg horizontal FOV).", "",
        "| Criterion | Threshold | Result | |", "|---|---|---|---|",
        f"| Mean absolute distance error | <= 15 cm | {mae:.1f} cm | {ok(mae <= 15)} |",
        f"| Detection rate (forward path) | >= 90 % | {det_rate:.1f} % | {ok(det_rate >= 90)} |",
        f"| End-to-end latency (estimated) | <= 300 ms | {latency:.0f} ms | {ok(latency <= 300)} |",
        f"| False positive rate (empty scene) | as low as possible | {fp:.1f} % of trials | |", "",
        "## Detail", "",
    ]
    for m in mats:
        for ln in lights:
            lines += [f"### {m}, {ln}", "", "| distance \\ azimuth | " + " | ".join(f"{a:+d}" for a in sorted(set(AZIMUTHS))) + " |",
                      "|---|" + "---|" * len(set(AZIMUTHS))]
            for d in DISTANCES:
                cells = []
                for a in sorted(set(AZIMUTHS)):
                    sel = [r for r in tgt if r["material"] == m and r["lighting"] == ln and r["distance_m"] == d and r["azimuth_deg"] == a]
                    dr, e = stats(sel)
                    zd = d * math.cos(math.radians(a))  # the Kinect limits z-depth, not radial distance
                    note = (" (centre outside FOV)" if abs(a) > half_fov else " (beyond max depth)" if zd > max_d
                            else " (inside min depth)" if zd < cfg["sensor"]["min_depth"] + 0.03 else "")
                    cells.append(f"{dr:.0f}%" + (f" / {e:.1f} cm" if dr > 0 else "") + note)
                lines.append(f"| {d:.1f} m | " + " | ".join(cells) + " |")
            lines.append("")
    lines += ["Cells: detection rate / mean absolute distance error. Conditions outside the sensor's range or field of "
              "view are reported, not excluded, as the protocol requires.", ""]
    return "\n".join(lines)
