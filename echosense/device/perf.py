"""Performance monitor: can this computer run Echo-Sense within the proposal's limits?

Samples once a second:

* pipeline - frames processed per second vs the sensor's rate, dropped frames,
  processing time, obstacle-to-sound latency, main-loop load, audio
  underruns and audio-thread load
* system   - CPU per core, CPU used by the device software, the Kinect driver
  process and (only in development) the simulator, memory, SoC temperature,
  CPU clock throttling, container CPU quota

and turns them into pass / warn / fail checks with an overall verdict.

The simulator's own CPU is reported separately and left out of the device
budget, because on the Orange Pi the Kinect driver (echosense-kinect2) takes its
place. Its cost is only known once a real Kinect runs on the board
(``echosense perf --source kinect2``).
"""
from __future__ import annotations

import csv
import os
import platform
import threading
import time
from collections import deque
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a dependency, keep running without it
    psutil = None

OK, WARN, FAIL, NA = "ok", "warn", "fail", "n/a"
RANK = {OK: 0, NA: 0, WARN: 1, FAIL: 2}
VERDICT = {OK: "Can handle it", WARN: "Marginal: little headroom", FAIL: "Overloaded"}


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def board_model() -> str:
    m = _read("/proc/device-tree/model")
    if m:
        return m.replace("\x00", "")
    return f"{platform.system()} {platform.machine()}"


def cgroup_quota() -> float | None:
    """CPUs available to this container (cgroup v2), or None when unlimited / not in a cgroup."""
    raw = _read("/sys/fs/cgroup/cpu.max")
    if not raw:
        return None
    q, p = raw.split()[:2]
    return None if q == "max" else int(q) / int(p)


def cgroup_usage_us() -> int | None:
    raw = _read("/sys/fs/cgroup/cpu.stat")
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    return None


def soc_temperature() -> float | None:
    """Hottest thermal zone in deg C (RK3588 exposes several: soc, bigcore0/1, littlecore, gpu, npu)."""
    best = None
    if psutil and hasattr(psutil, "sensors_temperatures"):
        try:
            for entries in psutil.sensors_temperatures().values():
                for e in entries:
                    if e.current and (best is None or e.current > best):
                        best = float(e.current)
        except Exception:
            pass
    if best is None:
        for z in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
            raw = _read(str(z))
            if raw and raw.lstrip("-").isdigit():
                t = int(raw) / 1000.0
                best = t if best is None else max(best, t)
    return best


def cpu_freqs() -> list[tuple[float, float]]:
    """(current, max) MHz per core from cpufreq, empty where the kernel hides it (macOS, containers)."""
    out = []
    for c in sorted(Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq")):
        cur, mx = _read(str(c / "scaling_cur_freq")), _read(str(c / "cpuinfo_max_freq"))
        if cur and mx:
            out.append((int(cur) / 1000, int(mx) / 1000))
    return out


class PerfMonitor:
    def __init__(self, app, interval: float = 1.0, history: int = 180, csv_path: str | None = None):
        self.app = app
        self.interval = interval
        self.history: deque = deque(maxlen=history)
        self.listeners: list = []
        self.latest: dict | None = None
        self.csv_path = csv_path
        self._csv = None
        self._procs: dict[int, "psutil.Process"] = {}
        self._run = False
        self._prev = None
        self._underrun_log: deque = deque()
        self.host = {"model": board_model(), "hostname": platform.node(), "arch": platform.machine(),
                     "cores": os.cpu_count(), "quota": cgroup_quota(), "python": platform.python_version(),
                     "profile": app.cfg.get("device_profile")}
        if psutil:
            self.me = psutil.Process(os.getpid())
            self.me.cpu_percent(None)
            psutil.cpu_percent(None, percpu=True)

    # ------------------------------------------------------------ control
    def start(self):
        self._run = True
        if self.csv_path:
            Path(self.csv_path).parent.mkdir(parents=True, exist_ok=True)
            self._csv = open(self.csv_path, "w", newline="")
        threading.Thread(target=self._loop, name="perf-monitor", daemon=True).start()

    def stop(self):
        self._run = False
        if self._csv:
            self._csv.close()
            self._csv = None

    def _loop(self):
        self._prev = self._counters()
        while self._run:
            time.sleep(self.interval)
            try:
                s = self.sample()
            except Exception as e:  # never take the device down for a metric
                print(f"[perf] sample failed: {e!r}")
                continue
            self.latest = s
            self.history.append(s)
            self._write_csv(s)
            self.app.logger.write("perf", **{k: v for k, v in s.items() if k not in ("checks", "cpu_cores")})
            for fn in list(self.listeners):
                fn(s)

    # ------------------------------------------------------------ sampling
    def _counters(self) -> dict:
        a = self.app
        return {"t": time.monotonic(), "processed": a.perf_processed, "dropped": a.perf_dropped,
                "busy": a.perf_busy_s, "underruns": a.engine.underruns, "cg": cgroup_usage_us()}

    def _cpu_by_role(self) -> dict:
        roles = {"device": 0.0, "driver": 0.0, "simulator": 0.0, "other": 0.0}
        if not psutil:
            return roles
        roles["device"] = self.me.cpu_percent(None)
        helper = self.app.helper_pids()
        try:
            kids = {p.pid: p for p in self.me.children(recursive=True)}
        except psutil.Error:
            kids = {}
        for pid, p in kids.items():
            if pid not in self._procs:
                self._procs[pid] = p
                try:
                    p.cpu_percent(None)
                except psutil.Error:
                    pass
                continue
            try:
                roles[helper.get(pid, "other")] += self._procs[pid].cpu_percent(None)
            except psutil.Error:
                pass
        for pid in list(self._procs):
            if pid not in kids:
                del self._procs[pid]
        return roles

    def sample(self) -> dict:
        a = self.app
        now = self._counters()
        prev, self._prev = self._prev, now
        dt = max(1e-3, now["t"] - prev["t"])
        processed = now["processed"] - prev["processed"]
        dropped = now["dropped"] - prev["dropped"]
        seen = processed + dropped
        m = a.metrics()
        self._underrun_log.append((now["t"], now["underruns"] - prev["underruns"]))
        while self._underrun_log and now["t"] - self._underrun_log[0][0] > 60:
            self._underrun_log.popleft()
        roles = self._cpu_by_role()
        cores = psutil.cpu_percent(None, percpu=True) if psutil else []
        vm = psutil.virtual_memory() if psutil else None
        freqs = cpu_freqs()
        cg = None
        if now["cg"] is not None and prev["cg"] is not None and self.host["quota"]:
            cg = 100 * (now["cg"] - prev["cg"]) / 1e6 / dt / self.host["quota"]
        s = {
            "t": round(time.time(), 2),
            "source": getattr(a.source, "kind", None),
            "fps": round(processed / dt, 1),
            "sensor_fps": a.cfg["sensor"]["fps"],
            "dropped_pct": round(100 * dropped / seen, 1) if seen else 0.0,
            "proc_mean": (m["process_ms"] or {}).get("mean"),
            "proc_p95": (m["process_ms"] or {}).get("p95"),
            "e2e_p95": (m["e2e_ms"] or {}).get("p95"),
            "e2e_mean": (m["e2e_ms"] or {}).get("mean"),
            "loop_load": round(100 * (now["busy"] - prev["busy"]) / dt, 1),
            "audio_load": round(100 * a.engine.render_load, 1),
            "underruns_60s": sum(n for _, n in self._underrun_log),
            "cpu_total": round(sum(cores) / len(cores), 1) if cores else None,
            "cpu_max_core": round(max(cores), 1) if cores else None,
            "cpu_cores": [round(c) for c in cores],
            "cpu_device": round(roles["device"], 1),
            "cpu_driver": round(roles["driver"], 1),
            "cpu_simulator": round(roles["simulator"], 1),
            "cpu_container": round(cg, 1) if cg is not None else None,
            "rss_mb": round(self.me.memory_info().rss / 2**20, 1) if psutil else None,
            "mem_avail_pct": round(vm.available / vm.total * 100, 1) if vm else None,
            "mem_total_mb": round(vm.total / 2**20) if vm else None,
            "temp_c": round(t, 1) if (t := soc_temperature()) is not None else None,
            "freq_mhz": round(sum(f[0] for f in freqs) / len(freqs)) if freqs else None,
            "throttled": bool(freqs) and any(c > 50 for c in cores) and any(cur < 0.85 * mx for cur, mx in freqs[-4:]),
        }
        s["checks"] = self.checks(s)
        worst = max((c["status"] for c in s["checks"]), key=lambda x: RANK[x], default=OK)
        s["status"], s["verdict"] = worst, VERDICT[worst]
        return s

    # ------------------------------------------------------------- verdict
    def checks(self, s: dict) -> list[dict]:
        cfg = self.app.cfg
        period_ms = 1000.0 / cfg["sensor"]["fps"]
        out = []

        def add(name, value, unit, status, target, hint=""):
            out.append({"name": name, "value": value, "unit": unit, "status": status, "target": target, "hint": hint})

        def grade(v, ok, warn, higher_is_better=False):
            if v is None:
                return NA
            if higher_is_better:
                return OK if v >= ok else WARN if v >= warn else FAIL
            return OK if v <= ok else WARN if v <= warn else FAIL

        fps_status = grade(s["fps"], 15, 10, True)
        fps_hint = "Proposal requires at least 15 fps; raise processing.stride if low"
        if fps_status != OK and s["source"] in ("sim", "replay") and s["dropped_pct"] <= 5:
            # every frame the source produced was processed: the simulator, not the device, is the limit
            fps_status = NA
            fps_hint = (f"info only: the simulator delivers only {s['fps']} fps on this computer and the device "
                        "software processed every frame")
        add("Frame rate", s["fps"], "fps", fps_status, ">= 15 fps", fps_hint)
        add("Keeping up with the sensor", s["dropped_pct"], "% dropped", grade(s["dropped_pct"], 5, 20), "<= 5 % dropped",
            "Frames arrive faster than they are processed")
        add("Processing time (p95)", s["proc_p95"], "ms", grade(s["proc_p95"], period_ms * 0.5, period_ms),
            f"<= {period_ms * 0.5:.0f} ms (half a frame)", "Time from depth frame to obstacle list")
        add("Obstacle-to-sound latency (p95)", s["e2e_p95"], "ms", grade(s["e2e_p95"], 250, 300), "<= 300 ms",
            "Measured on every new obstacle; needs obstacles to appear")
        add("Audio dropouts (last 60 s)", s["underruns_60s"], "underruns", grade(s["underruns_60s"], 0, 2), "0",
            "Clicks or gaps in the cue sound")
        add("Audio thread load", s["audio_load"], "%", grade(s["audio_load"], 40, 70), "<= 40 %")
        dev = (s["cpu_device"] or 0) + (s["cpu_driver"] or 0)
        add("Device software CPU", round(dev, 1), "% of one core", grade(dev, 60, 85), "<= 60 %",
            "Echo-Sense + Kinect driver, simulator excluded. The Python pipeline mostly runs on one core")
        # Whole-machine CPU only says something about the board when nothing but Echo-Sense runs on
        # it. With the simulator (or other apps on a laptop) it is shown for information only.
        dev_run = s["source"] in ("sim", "replay") or platform.system() != "Linux"
        why = "info only: includes the simulator / other apps on this computer" if dev_run else ""
        add("Busiest CPU core", s["cpu_max_core"], "%", NA if dev_run else grade(s["cpu_max_core"], 80, 95), "<= 80 %",
            why or "One saturated core stalls the pipeline even when the total looks low")
        tot = s["cpu_container"] if s["cpu_container"] is not None else s["cpu_total"]
        add("Total CPU", tot, "%", NA if dev_run else grade(tot, 70, 90), "<= 70 %",
            why or ("Container quota" if s["cpu_container"] is not None else "All cores"))
        add("SoC temperature", s["temp_c"], "°C", grade(s["temp_c"], 75, 85), "<= 75 °C",
            "RK3588 starts throttling near 85 °C; a heatsink or fan may be needed in the housing")
        add("CPU clock throttling", "yes" if s["throttled"] else ("no" if s["freq_mhz"] else None), "",
            WARN if s["throttled"] else (OK if s["freq_mhz"] else NA), "no", "Cores running below max clock under load")
        linux = platform.system() == "Linux"
        add("Free memory", s["mem_avail_pct"], "%", grade(s["mem_avail_pct"], 25, 10, True) if linux else NA, ">= 25 %",
            "" if linux else "info only: macOS counts cached memory as used")
        add("Device memory", s["rss_mb"], "MB", grade(s["rss_mb"], 400, 800), "<= 400 MB")
        return out

    def summary(self) -> dict:
        """Worst / mean over the history - what an endurance run reports."""
        h = list(self.history)
        if not h:
            return {}

        def col(k):
            return [x[k] for x in h if isinstance(x.get(k), (int, float))]

        def mean(k):
            v = col(k)
            return round(sum(v) / len(v), 1) if v else None

        def mx(k):
            v = col(k)
            return max(v) if v else None

        worst = max((x["status"] for x in h), key=lambda x: RANK[x])
        return {"samples": len(h), "status": worst, "verdict": VERDICT[worst],
                "fps_mean": mean("fps"), "fps_min": min(col("fps")) if col("fps") else None,
                "dropped_pct_mean": mean("dropped_pct"), "proc_p95_max": mx("proc_p95"), "e2e_p95_max": mx("e2e_p95"),
                "cpu_max_core_max": mx("cpu_max_core"), "cpu_device_mean": mean("cpu_device"),
                "cpu_driver_mean": mean("cpu_driver"), "cpu_simulator_mean": mean("cpu_simulator"),
                "temp_max": mx("temp_c"), "rss_mb_max": mx("rss_mb"), "underruns": sum(col("underruns_60s")[-1:]) if h else 0,
                "status_counts": {k: sum(1 for x in h if x["status"] == k) for k in (OK, WARN, FAIL)}}

    def _write_csv(self, s: dict):
        if not self._csv:
            return
        row = {k: v for k, v in s.items() if k not in ("checks", "cpu_cores")}
        if not hasattr(self, "_writer"):
            self._writer = csv.DictWriter(self._csv, fieldnames=list(row.keys()))
            self._writer.writeheader()
        self._writer.writerow(row)
        self._csv.flush()
