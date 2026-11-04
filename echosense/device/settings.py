"""User settings that survive power cycles (proposal A12 / C11)."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

RANGES = ("near", "medium", "far")
MODES = ("continuous", "warning")


@dataclass
class UserSettings:
    power: bool = True
    volume: int = 6  # 0..10
    range: str = "medium"  # near | medium | far
    mode: str = "continuous"  # continuous | warning
    warning_distance: float = 1.2
    profile: str = "tone"  # tone | click | soft
    report_off_path: bool = False
    logging: bool = False  # session logging is for the evaluation period only
    announce: bool = False  # say "Person ahead" / "Vehicle left" automatically when one appears
    # device fitting (caregiver): chest-mount height and tilt; None = profile default
    mount_height: Optional[float] = None
    mount_pitch: Optional[float] = None


class SettingsStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.s = UserSettings()
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text())
            names = {f.name for f in fields(UserSettings)}
            self.s = UserSettings(**{k: v for k, v in data.items() if k in names})
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[settings] could not read {self.path}: {e}; using defaults")
        self._sanitize()

    def _sanitize(self):
        s = self.s
        s.volume = int(min(10, max(0, s.volume)))
        if s.range not in RANGES:
            s.range = "medium"
        if s.mode not in MODES:
            s.mode = "continuous"

    def save(self):
        with self._lock:
            self._sanitize()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.s), indent=2))
            os.replace(tmp, self.path)  # atomic: a power cut never leaves a half-written file

    def update(self, **kw):
        for k, v in kw.items():
            if hasattr(self.s, k):
                setattr(self.s, k, v)
        self.save()

    def to_dict(self):
        return asdict(self.s)
