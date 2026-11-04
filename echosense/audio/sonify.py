"""Obstacle -> 3D sound mapping (proposal section 5, system functions C5/C6).

Mapping (fixed during development, identical for every user):

* **Direction** - the cue is rendered through the HRTF at the obstacle's
  azimuth and elevation relative to the user's ears.
* **Distance** - nearer = faster repetition and louder (a "parking sensor"
  metaphor most people already know). Inside ``urgent_distance`` the pings
  merge into a rapid burst.
* **Kind / height band** - pitch and pattern:
    - body-height obstacle: mid tone
    - low obstacle (tripping hazard): low tone
    - head-level overhang (what the cane cannot find): high double-ping
    - drop-off (hole, open drainage, curb): falling "dip" sweep

Sound profiles change the timbre only ("tone" = harmonic tone with a click
onset, "click" = broadband clicks which localise better, "soft" = gentle sine).
"""
from __future__ import annotations

import math
import threading
import time

import numpy as np

from .engine import AudioEngine
from .hrtf import StructuralHRTF

PROFILES = ("tone", "click", "soft")
KIND_PITCH = {"body": 620.0, "low": 360.0, "head": 1150.0, "drop": 520.0}


def _env(n: int, sr: int, attack=0.003, release=0.03) -> np.ndarray:
    e = np.ones(n, np.float32)
    a, r = max(1, int(attack * sr)), max(1, int(release * sr))
    e[:a] = np.linspace(0, 1, a)
    e[-r:] *= np.linspace(1, 0, r) ** 2
    return e


def synth_ping(kind: str, profile: str, sr: int, rng: np.random.Generator | None = None) -> np.ndarray:
    rng = rng or np.random.default_rng(3)
    f0 = KIND_PITCH.get(kind, 620.0)

    def tone(freq, dur, harmonics=(1.0, 0.45, 0.2), sweep_to=None):
        n = int(dur * sr)
        t = np.arange(n) / sr
        if sweep_to:
            ph = 2 * np.pi * (freq * t + (sweep_to - freq) * t * t / (2 * dur))
        else:
            ph = 2 * np.pi * freq * t
        y = sum(a * np.sin((k + 1) * ph) for k, a in enumerate(harmonics))
        return (y * _env(n, sr)).astype(np.float32)

    def click(freq, dur):
        n = int(dur * sr)
        noise = rng.standard_normal(n).astype(np.float32)
        # crude band emphasis: noise ring-modulated by the pitch, fast decay
        t = np.arange(n) / sr
        y = noise * (0.5 + 0.5 * np.sin(2 * np.pi * freq * t)) * np.exp(-t / (dur / 3))
        return (y * _env(n, sr, 0.0005, 0.01)).astype(np.float32)

    if profile == "click":
        if kind == "head":
            a = click(f0 * 2, 0.03)
            return np.concatenate([a, np.zeros(int(0.05 * sr), np.float32), a]) * 0.8
        if kind == "drop":
            return np.concatenate([click(f0, 0.03), np.zeros(int(0.03 * sr), np.float32), click(f0 * 0.5, 0.05)])
        return click(f0 * (1.5 if kind == "body" else 1.0), 0.04)

    soft = profile == "soft"
    harm = (1.0,) if soft else (1.0, 0.45, 0.2)
    if kind == "head":
        a = tone(f0, 0.045, harm)
        y = np.concatenate([a, np.zeros(int(0.04 * sr), np.float32), a])
    elif kind == "drop":
        y = tone(f0, 0.16, harm, sweep_to=f0 * 0.45)
    else:
        y = tone(f0, 0.07 if kind == "body" else 0.09, harm)
    if not soft:  # short broadband onset helps localisation
        n = min(len(y), int(0.004 * sr))
        y[:n] += rng.standard_normal(n).astype(np.float32) * np.linspace(0.6, 0, n, dtype=np.float32)
    return y * (0.6 if soft else 1.0)


class Sonifier:
    """Engine generator that turns the current cue list into spatialised pings."""

    def __init__(self, sr: int, hrtf: StructuralHRTF, range_limit: float = 2.5, profile: str = "tone",
                 urgent_distance: float = 0.6, on_ping=None):
        self.sr = sr
        self.hrtf = hrtf
        self.range_limit = range_limit
        self.profile = profile
        self.urgent = urgent_distance
        self.enabled = True
        self.on_ping = on_ping  # callback(cue, onset_wall_time)
        self._cues: list[dict] = []
        self._voices: dict = {}
        self._ping_cache: dict = {}
        self._lock = threading.Lock()
        self.pings = 0

    def set_cues(self, cues: list[dict]):
        with self._lock:
            self._cues = list(cues)

    def ping(self, kind: str) -> np.ndarray:
        key = (kind, self.profile)
        if key not in self._ping_cache:
            self._ping_cache[key] = synth_ping(kind, self.profile, self.sr)
        return self._ping_cache[key]

    def interval(self, r: float, rank: int) -> float:
        if r <= self.urgent:
            base = 0.075
        else:
            x = min(1.0, max(0.0, (r - self.urgent) / max(0.3, self.range_limit - self.urgent)))
            base = 0.11 + 0.79 * x ** 1.2
        return base * (1.35 if rank else 1.0)

    def gain(self, r: float, rank: int) -> float:
        x = min(1.0, max(0.0, (r - 0.5) / max(0.3, self.range_limit - 0.5)))
        return (1.0 - 0.6 * x) * (0.45 if rank else 1.0)

    def render_cue(self, cue: dict) -> np.ndarray:
        dry = self.ping(cue["kind"])
        h = self.hrtf.hrir(cue["az"], cue.get("el", 0.0))
        wet = np.stack([np.convolve(dry, h[:, 0]), np.convolve(dry, h[:, 1])], 1)
        return wet * self.gain(cue["r"], cue.get("rank", 0)) * 0.5

    def __call__(self, engine: AudioEngine, start: int, n: int):
        with self._lock:
            cues = self._cues if self.enabled else []
        end = start + n
        live = set()
        for cue in cues:
            key = cue.get("id")
            live.add(key)
            v = self._voices.get(key)
            if v is None:
                v = self._voices[key] = {"next": start, "first": True}
            if v["next"] < start:
                v["next"] = start
            while v["next"] < end:
                engine.schedule(self.render_cue(cue), at_sample=v["next"])
                self.pings += 1
                if self.on_ping:
                    self.on_ping(cue, engine.wall_time_of(v["next"]), v["first"])
                v["first"] = False
                v["next"] += int(self.interval(cue["r"], cue.get("rank", 0)) * self.sr)
        for key in list(self._voices):
            if key not in live:
                del self._voices[key]


# ---------------------------------------------------------------- earcons
def earcon(name: str, sr: int) -> np.ndarray:
    def t(freq, dur, amp=0.35):
        n = int(dur * sr)
        x = np.arange(n) / sr
        return (amp * np.sin(2 * np.pi * freq * x) * _env(n, sr, 0.004, 0.02)).astype(np.float32)

    gap = lambda d: np.zeros(int(d * sr), np.float32)  # noqa: E731
    if name == "confirm":
        y = np.concatenate([t(660, 0.06), gap(0.02), t(880, 0.08)])
    elif name == "decline":
        y = np.concatenate([t(880, 0.06), gap(0.02), t(660, 0.08)])
    elif name == "limit":
        y = t(440, 0.12)
    elif name == "power_on":
        y = np.concatenate([t(523, 0.09), t(659, 0.09), t(784, 0.14)])
    elif name == "power_off":
        y = np.concatenate([t(784, 0.09), t(659, 0.09), t(523, 0.14)])
    elif name == "alarm":
        y = np.concatenate([t(1400, 0.08, 0.5), gap(0.05)] * 3)
    elif name == "contact":
        y = t(180, 0.12, 0.5)
    else:
        y = t(700, 0.08)
    return y
