"""Physical buttons on the Orange Pi (libgpiod).

Each tactile button pulls a GPIO line to ground. Lines are polled with
debounce and turned into the same actions the web console sends, so the
device behaves identically whichever way it is controlled.

The pin map lives in the profile (``"gpio": {"chip": "gpiochip1", "buttons":
{"power": 12, ...}}``). Line numbers depend on the board and the header pins
you wire; check them with ``gpioinfo`` on the Orange Pi. Untested until the
hardware is assembled (Sprint 3).
"""
from __future__ import annotations

import threading
import time


class GpioButtons:
    def __init__(self, cfg: dict, on_action):
        import gpiod  # python3-libgpiod (v1 API, as shipped with Ubuntu 22.04)

        self.gpiod = gpiod
        self.cfg = cfg
        self.on_action = on_action
        self.chip = gpiod.Chip(cfg.get("chip", "gpiochip0"))
        self.lines = {}
        for action, offset in cfg.get("buttons", {}).items():
            line = self.chip.get_line(int(offset))
            line.request(consumer="echosense", type=gpiod.LINE_REQ_DIR_IN,
                         flags=getattr(gpiod, "LINE_REQ_FLAG_BIAS_PULL_UP", 0))
            self.lines[action] = line
        self._run = True
        threading.Thread(target=self._loop, name="gpio", daemon=True).start()

    def _loop(self):
        state = {a: 1 for a in self.lines}
        since = {a: 0.0 for a in self.lines}
        while self._run:
            now = time.monotonic()
            for a, line in self.lines.items():
                v = line.get_value()
                if v != state[a] and now - since[a] > 0.05:
                    state[a], since[a] = v, now
                    if v == 0:  # pressed (active low)
                        self.on_action(a, "gpio")
            time.sleep(0.01)

    def stop(self):
        self._run = False


def start_gpio(cfg: dict, on_action):
    if not cfg or not cfg.get("buttons"):
        return None
    try:
        return GpioButtons(cfg, on_action)
    except Exception as e:
        print(f"[gpio] buttons disabled: {e}")
        return None
