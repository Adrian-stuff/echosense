"""``echosense perf``: run the device software headless and report whether this computer keeps up.

Meant for SSH on the Orange Pi (and for the Docker virtual Orange Pi):

    echosense perf --source kinect2 --minutes 10      # the real answer, on the board with the Kinect
    echosense perf --minutes 30                         # endurance / thermal run with the simulator

Prints a line every 5 seconds, then the checks and a summary, writes every
sample to a CSV, and exits 0 (handles it), 1 (marginal) or 2 (overloaded) so it
can be scripted.
"""
from __future__ import annotations

import platform
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from ..config import load_config
from ..device.perf import RANK, VERDICT

WARMUP_S = 4
HIGHER_IS_BETTER = {"Frame rate", "Free memory"}


def _worse(a: dict, b: dict) -> bool:
    va, vb = a["value"], b["value"]
    if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
        return False
    return va < vb if a["name"] in HIGHER_IS_BETTER else va > vb


def run(args):
    from ..app import EchoSense

    cfg = load_config(args.config, args.profile)
    seconds = args.seconds or int(args.minutes * 60)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = args.csv or str(out / f"perf-{platform.node()}-{datetime.now():%Y%m%d-%H%M%S}.csv")
    settings = str(Path(tempfile.mkdtemp()) / "settings.json")  # never touch the wearer's saved settings
    app = EchoSense(cfg, source=args.source, scene=args.scene, audio=args.audio, settings_path=settings, perf_csv=csv_path)
    app.start()
    if app.sim:
        app.sim.autowalk = True  # obstacles keep appearing, so latency is measured too
    h = app.perf.host
    quota = f", container quota {h['quota']} CPUs" if h["quota"] else ""
    print(f"\nEcho-Sense performance check: {h['model']} ({h['hostname']}), {h['cores']} cores{quota}, "
          f"profile {h['profile']}, source {getattr(app.source, 'kind', '?')}, {seconds} s\n")
    print(f"{'time':>6} {'fps':>5} {'drop%':>6} {'proc95':>7} {'e2e95':>6} {'devCPU':>7} {'core%':>6} {'temp':>5}  verdict")
    t0 = time.monotonic()
    try:
        while time.monotonic() - t0 < seconds:
            time.sleep(5)
            p = app.perf.latest
            if not p:
                continue
            f = lambda v, fmt: "—" if v is None else format(v, fmt)  # noqa: E731
            print(f"{time.monotonic() - t0:>5.0f}s {f(p['fps'], '.1f'):>5} {f(p['dropped_pct'], '.1f'):>6} "
                  f"{f(p['proc_p95'], '.1f'):>7} {f(p['e2e_p95'], '.0f'):>6} "
                  f"{f((p['cpu_device'] or 0) + (p['cpu_driver'] or 0), '.0f'):>7} {f(p['cpu_max_core'], '.0f'):>6} "
                  f"{f(p['temp_c'], '.0f'):>5}  {p['verdict']}", flush=True)
    except KeyboardInterrupt:
        print("stopped early")
    finally:
        app.stop()

    hist = [s for s in app.perf.history if s["t"] - app.perf.history[0]["t"] >= WARMUP_S] if app.perf.history else []
    if not hist:
        print("no samples collected")
        return sys.exit(2)
    worst_by_check: dict[str, dict] = {}
    for s in hist:
        for c in s["checks"]:
            w = worst_by_check.get(c["name"])
            if w is None or RANK[c["status"]] > RANK[w["status"]] or (
                    RANK[c["status"]] == RANK[w["status"]] and _worse(c, w)):
                worst_by_check[c["name"]] = c
    print(f"\nWorst result per check over {len(hist)} samples (first {WARMUP_S} s of warm-up ignored):")
    for c in worst_by_check.values():
        val = "—" if c["value"] is None else f"{c['value']} {c['unit']}"
        print(f"  [{c['status']:>4}] {c['name']:<34} {val:<22} target {c['target']}")
    status = max((c["status"] for c in worst_by_check.values()), key=lambda x: RANK[x])
    app.perf.history.clear()
    app.perf.history.extend(hist)
    sm = app.perf.summary()
    print(f"\nfps mean {sm['fps_mean']} (min {sm['fps_min']}) · processing p95 max {sm['proc_p95_max']} ms · "
          f"latency p95 max {sm['e2e_p95_max']} ms · device CPU mean {sm['cpu_device_mean']}% of a core"
          + (f" · Kinect driver {sm['cpu_driver_mean']}%" if sm["cpu_driver_mean"] else "")
          + (f" · simulator {sm['cpu_simulator_mean']}% (excluded)" if sm["cpu_simulator_mean"] else "")
          + (f" · max temp {sm['temp_max']} °C" if sm["temp_max"] is not None else ""))
    if getattr(app.source, "kind", None) == "sim":
        print("Note: simulator run. The Kinect v2 driver's CPU cost is not included; repeat with --source kinect2 on the board.")
    print(f"\nVERDICT: {VERDICT[status]}\nSamples: {csv_path}\n")
    sys.exit(RANK[status])
