"""Per-stage timing against the proposal's 300 ms end-to-end budget.

Run it on the laptop, inside the Docker virtual Orange Pi, and on the real
board; the numbers are only meaningful on the real board, the others show
trends and regressions.
"""
from __future__ import annotations

import os
import platform
import time

import numpy as np

from ..audio.engine import AudioEngine
from ..audio.hrtf import StructuralHRTF
from ..audio.sonify import Sonifier
from ..config import load_config
from ..processing.pipeline import DepthProcessor
from ..sensors.base import DepthFrame
from ..sensors.simulated import SimulatedKinect


def cpu_quota() -> str:
    try:
        q, p = open("/sys/fs/cgroup/cpu.max").read().split()
        return "unlimited" if q == "max" else f"{int(q) / int(p):.2f} CPUs"
    except Exception:
        return "n/a"


def pct(v, q):
    return float(np.percentile(v, q)) if len(v) else float("nan")


def run(args):
    cfg = load_config(args.config, args.profile)
    sim = SimulatedKinect(cfg, args.scene, seed=1)
    sim.autowalk = True
    proc = DepthProcessor(cfg)
    a = cfg["audio"]
    eng = AudioEngine(a["sample_rate"], a["block_size"], a["max_output_dbfs"])
    son = Sonifier(a["sample_rate"], StructuralHRTF(a["sample_rate"]), cfg["ranges"]["medium"])
    eng.generators.append(son)
    period = 1.0 / cfg["sensor"]["fps"]
    t_sim, t_proc, t_audio, n_cues = [], [], [], []
    acc = 0.0
    for i in range(args.frames):
        sim._step(period, i * period)
        t0 = time.perf_counter()
        depth, extra = sim.capture(i * period)
        t1 = time.perf_counter()
        res = proc.process(DepthFrame(depth, i * period, i + 1, sim.intrinsics, "sim", extra), cfg["ranges"]["medium"])
        t2 = time.perf_counter()
        son.set_cues([{**o, "rank": k} for k, o in enumerate(res.reported[: a["max_cues"]])])
        acc += period * eng.sr
        blocks = 0
        while acc >= eng.block:
            eng.render(eng.block)
            acc -= eng.block
            blocks += 1
        t3 = time.perf_counter()
        t_sim.append((t1 - t0) * 1000)
        t_proc.append((t2 - t1) * 1000)
        t_audio.append((t3 - t2) * 1000)
        n_cues.append(len(res.reported))

    print(f"\nEcho-Sense pipeline benchmark  ({args.frames} frames, scene {args.scene}, profile {cfg['device_profile']})")
    print(f"host  {platform.node()}  {platform.system()} {platform.machine()}  cpus={os.cpu_count()}  quota={cpu_quota()}"
          f"  python {platform.python_version()}  numpy {np.__version__}")
    print(f"stride {cfg['processing']['stride']}  confirm_frames {cfg['processing']['confirm_frames']} (+sensitivity)  "
          f"audio block {a['block_size']} @ {a['sample_rate']} Hz\n")
    print(f"{'stage':<28}{'mean':>8}{'p95':>8}{'max':>8}   ms")
    for name, v in (("simulated sensor (not on device)", t_sim), ("processing", t_proc), ("audio render / frame", t_audio)):
        print(f"{name:<28}{np.mean(v):>8.2f}{pct(v, 95):>8.2f}{np.max(v):>8.2f}")
    load = (np.mean(t_proc) + np.mean(t_audio)) / (period * 1000)
    print(f"\nCPU share for one {cfg['sensor']['fps']} fps stream: {load * 100:.0f}% of one core "
          f"(Kinect decoding by libfreenect2 comes on top of this on the device)")

    from ..processing.pipeline import DepthProcessor as _DP
    cf = _DP(cfg).frames_to_confirm(cfg["sensor"]["fps"])  # includes the default sensitivity level
    budget = [
        ("sensor exposure + transfer (assumed 1 frame)", period * 1000),
        (f"confirmation ({cf - 1} more frame{'s' if cf != 2 else ''})", (cf - 1) * period * 1000),
        ("processing p95", pct(t_proc, 95)),
        ("audio block scheduling", a["block_size"] / a["sample_rate"] * 1000),
        ("audio device output (assumed)", 10.0),
    ]
    total = sum(v for _, v in budget)
    print("\nEstimated obstacle-to-sound latency")
    for n, v in budget:
        print(f"  {n:<46}{v:>7.1f} ms")
    verdict = "within" if total <= 300 else "OVER"
    print(f"  {'total':<46}{total:>7.1f} ms   ({verdict} the 300 ms budget)\n")
    print("Measure the real figure on the device: the console's 'e2e' readout times every first cue,")
    print("and the sensor's own latency still has to be measured with the protocol's test rig.\n")
