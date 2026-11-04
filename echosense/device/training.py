"""Training and familiarisation mode (proposal A11 / B6).

A single stationary virtual obstacle is played at a chosen direction and
distance without walking. The supervisor can reveal the target or run a blind
trial in which the user points to where the sound comes from; the angular
error of each response is recorded (the proposal's angular localisation error).
"""
from __future__ import annotations

import random
import statistics
import time

PROTOCOL_AZIMUTHS = (0, -15, 15, -30, 30, -45, 45, -60, 60)
PROTOCOL_DISTANCES = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)


class Trainer:
    def __init__(self):
        self.active = False
        self.target = {"az": 0.0, "r": 1.5, "kind": "body", "el": 0.0}
        self.blind = False
        self.trials: list[dict] = []
        self._started = None

    def start(self):
        self.active = True
        self._started = time.monotonic()

    def stop(self):
        self.active = False

    def set_target(self, az=None, r=None, kind=None, el=None, blind=False):
        if az is not None:
            self.target["az"] = float(az)
        if r is not None:
            self.target["r"] = float(r)
        if kind is not None:
            self.target["kind"] = kind
        if el is not None:
            self.target["el"] = float(el)
        self.blind = blind
        self._started = time.monotonic()

    def random_target(self):
        self.set_target(az=random.choice(PROTOCOL_AZIMUTHS), r=random.choice(PROTOCOL_DISTANCES[1:5]),
                        kind=random.choice(("body", "body", "head", "low")), blind=True)

    def respond(self, az_pointed: float) -> dict:
        err = abs(((float(az_pointed) - self.target["az"]) + 180) % 360 - 180)
        trial = {"target_az": self.target["az"], "target_r": self.target["r"], "kind": self.target["kind"],
                 "response_az": round(float(az_pointed), 1), "error_deg": round(err, 1),
                 "response_time_s": round(time.monotonic() - (self._started or time.monotonic()), 2)}
        self.trials.append(trial)
        self.blind = False
        return trial

    def cues(self) -> list[dict]:
        if not self.active:
            return []
        t = self.target
        return [{"id": "training", "kind": t["kind"], "r": t["r"], "az": t["az"], "el": t.get("el", 0.0), "rank": 0}]

    def summary(self) -> dict:
        errs = [t["error_deg"] for t in self.trials]
        return {"trials": len(errs), "mae_deg": round(statistics.mean(errs), 1) if errs else None,
                "last": self.trials[-5:]}

    def to_dict(self) -> dict:
        tgt = None if self.blind else self.target
        return {"active": self.active, "blind": self.blind, "target": tgt, "summary": self.summary()}
