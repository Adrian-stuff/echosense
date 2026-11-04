"""Sensor health (fail-safe), battery, and session logging."""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path


class HealthMonitor:
    """Detects a blocked lens, a silent / disconnected sensor, and degraded sensing (proposal C8).

    States: ``starting`` -> ``ok`` | ``degraded`` | ``blocked`` | ``no_data``.
    ``degraded`` means the sensor works but loses many returns (direct sunlight, a dirty lens, very
    dark or shiny surroundings): cues continue and the user is told to take extra care.
    ``tick`` returns the new state on a transition, ``"repeat"`` when an active fault should be
    announced again, otherwise None.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.reset()

    def reset(self):
        self.state = "starting"
        self.last_frame = None
        self.low_since = None
        self.poor_since = None
        self.last_alert = 0.0
        self.valid_ratio = 0.0
        self.quality = 1.0

    def on_frame(self, t: float, valid_ratio: float, quality: float = 1.0):
        self.last_frame = t
        self.valid_ratio = valid_ratio
        self.quality += 0.2 * (quality - self.quality)  # smoothed: flicker alone is not a fault
        if valid_ratio < self.cfg["blocked_valid_ratio"]:
            self.low_since = self.low_since or t
        else:
            self.low_since = None
        thr = self.cfg.get("degraded_quality", 0.6) + (0.1 if self.state == "degraded" else 0.0)  # hysteresis
        if self.quality < thr:
            self.poor_since = self.poor_since or t
        else:
            self.poor_since = None

    def tick(self, t: float):
        c = self.cfg
        if self.last_frame is None or t - self.last_frame > c["no_data_timeout"]:
            new = "no_data" if (self.last_frame is not None or self.state != "starting") else "starting"
        elif self.low_since is not None and t - self.low_since > c["blocked_time"]:
            new = "blocked"
        elif self.poor_since is not None and t - self.poor_since > c.get("degraded_time", 1.5):
            new = "degraded"
        else:
            new = "ok"
        if new != self.state:
            self.state = new
            self.last_alert = t
            return new
        if new in ("blocked", "no_data") and t - self.last_alert > c["alert_repeat"]:
            self.last_alert = t
            return "repeat"
        if new == "degraded" and t - self.last_alert > 4 * c["alert_repeat"]:
            self.last_alert = t
            return "repeat"
        return None


class Battery:
    """Battery state. Simulated until the fuel-gauge hardware is chosen.

    Replace ``level`` with a reading from the power module (e.g. an INA219 /
    MAX17048 over I2C) on the real device; warnings and announcements stay the same.
    """

    def __init__(self, cfg: dict, level: float = 100.0):
        self.cfg = cfg
        self.level = float(level)
        self.drain_per_s = 100.0 / (cfg["capacity_hours"] * 3600)
        self.charging = False
        self._t = time.monotonic()
        self._warned = set()

    def update(self):
        now = time.monotonic()
        dt, self._t = now - self._t, now
        self.level = min(100.0, self.level + dt * self.drain_per_s * 4) if self.charging else max(0.0, self.level - dt * self.drain_per_s)
        for thr, kind in ((self.cfg["critical_warning"], "battery_critical"), (self.cfg["low_warning"], "battery_low")):
            if self.level <= thr and thr not in self._warned:
                self._warned.add(thr)
                return kind
        self._warned = {t for t in self._warned if self.level <= t + 2}
        return None

    def set(self, level: float):
        self.level = max(0.0, min(100.0, float(level)))
        self._warned = {t for t in self._warned if self.level <= t}

    @property
    def percent(self) -> int:
        return int(round(self.level))


class SessionLogger:
    """Plain-text JSON-lines session records for the evaluation (proposal C12).

    Contains detections, cues, settings and timings only - never anything
    that identifies the respondent. Disabled unless switched on.
    """

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        self.f = None
        self.path = None
        self.t0 = time.monotonic()

    @property
    def active(self):
        return self.f is not None

    def start(self, meta: dict):
        self.stop()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / f"session-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
        self.f = open(self.path, "a", buffering=1)
        self.t0 = time.monotonic()
        self.write("session_start", **meta)

    def write(self, kind: str, **data):
        if not self.f:
            return
        rec = {"t": round(time.monotonic() - self.t0, 4), "type": kind, **data}
        self.f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")

    def stop(self):
        if self.f:
            self.write("session_end")
            self.f.close()
            self.f = None
