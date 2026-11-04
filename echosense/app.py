"""Echo-Sense device application: sensing -> processing -> audio, plus power & control.

This is the program that runs on the Orange Pi. On a laptop it runs the same
way against the simulator; in Docker it runs inside the virtual Orange Pi.
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from collections import deque

import numpy as np

from .audio.engine import AudioEngine, BrowserTap, make_driver
from .audio.hrtf import StructuralHRTF
from .audio.sonify import PROFILES, Sonifier, earcon
from .audio.speech import Speech
from .config import expand
from .device.gpio import start_gpio
from .device.monitors import Battery, HealthMonitor, SessionLogger
from .device.settings import MODES, RANGES, SettingsStore
from .device.training import Trainer
from .processing.pipeline import DepthProcessor
from .sim.world import list_scenes
from .sensors import Recorder, SimulatedKinect, SourceError, available_sources, open_source


def volume_gain(v: int) -> float:
    return 0.0 if v <= 0 else 10 ** (((v - 10) * 2.5) / 20)


class EchoSense:
    def __init__(self, cfg: dict, source: str = "auto", scene: str = "maysan_street", audio: str | None = None,
                 record: str | None = None, settings_path: str | None = None):
        self.cfg = cfg
        self.scene = scene
        self.source_request = source
        self.audio_kind = audio or cfg["audio"]["driver"]
        self.record_path = record
        self.settings = SettingsStore(expand(settings_path or cfg["paths"]["settings"]))
        s = self.settings.s
        a = cfg["audio"]
        self.engine = AudioEngine(a["sample_rate"], a["block_size"], a["max_output_dbfs"])
        self.engine.master_gain = volume_gain(s.volume)
        self.hrtf = StructuralHRTF(a["sample_rate"], a["head_radius"])
        self.sonifier = Sonifier(a["sample_rate"], self.hrtf, cfg["ranges"][s.range], s.profile, on_ping=self._on_ping)
        self.engine.generators.append(self.sonifier)
        self.browser_tap = BrowserTap(self.engine)
        self.events: deque = deque(maxlen=200)
        self.event_listeners: list = []
        self.frame_listeners: list = []
        self.speech = Speech(self.engine, cfg["speech"], on_say=lambda txt: self.event("speech", txt))
        self.health = HealthMonitor(cfg["health"])
        self.battery = Battery(cfg["battery"])
        self.logger = SessionLogger(expand(cfg["paths"]["logs"]))
        self.trainer = Trainer()
        self.processor = DepthProcessor(cfg)
        self.source = None
        self.driver = None
        self.gpio = None
        self.recorder = None
        self.running = False
        self._source_lock = threading.Lock()
        self._last_packet = 0.0
        self._contacts_seen = 0
        self._pinged_tracks: set = set()
        self.m_frames: deque = deque(maxlen=60)
        self.m_proc: deque = deque(maxlen=120)
        self.m_e2e: deque = deque(maxlen=50)
        self.m_age: deque = deque(maxlen=120)
        self.cues: list = []
        self.last_result = None
        self._apply_mount()

    # ============================================================ lifecycle
    def start(self):
        self.running = True
        self.driver = make_driver(self.engine, self.audio_kind)
        self.event("info", f"Audio output: {self.driver.name}")
        self.switch_source(self.source_request)
        self.gpio = start_gpio(self.cfg.get("gpio"), self.action)
        if self.settings.s.logging:
            self._start_log()
        threading.Thread(target=self._loop, name="echosense-main", daemon=True).start()
        if self.settings.s.power:
            threading.Thread(target=self._self_test, name="self-test", daemon=True).start()

    def stop(self):
        self.running = False
        if self.source:
            self.source.stop()
        if self.driver:
            self.driver.stop()
        if self.recorder:
            self.recorder.close()
        self.logger.stop()

    # ============================================================== sources
    def switch_source(self, name: str) -> bool:
        with self._source_lock:
            old = self.source
            same = old is not None and (old.kind == name or name.startswith("replay:") and old.kind == "replay")
            if same:
                old.stop()  # a device can only be opened once
            try:
                src = open_source(name, self.cfg, self.scene)
            except SourceError as e:
                self.event("error", f"Could not open {name}: {e}")
                if old is not None and not same:
                    return False  # keep the current source running
                src = open_source("sim", self.cfg, self.scene)
            if old is not None and not same and old is not src:
                old.stop()
            self.source = src
            if src.error:
                self.event("warning", src.error)
            self.processor.reset()
            self.health.reset()
            self._pinged_tracks.clear()
            self._contacts_seen = 0
            self._apply_mount()
            if self.record_path and src is not old:
                if self.recorder:
                    self.recorder.close()
                self.recorder = Recorder(self.record_path, src.intrinsics, src.kind)
                self.event("info", f"Recording depth to {self.record_path}")
            self.event("source", f"Depth source: {src.label}", source=src.kind)
            self.logger.write("source", source=src.kind)
            return src.kind == name or name == "auto"

    @property
    def sim(self) -> SimulatedKinect | None:
        return self.source if isinstance(self.source, SimulatedKinect) else None

    def _apply_mount(self):
        s = self.settings.s
        h = s.mount_height if s.mount_height is not None else self.cfg["sensor"]["mount_height"]
        p = s.mount_pitch if s.mount_pitch is not None else self.cfg["sensor"]["mount_pitch_deg"]
        self.processor.set_mount(h, p)
        if self.sim:
            self.sim.mount_height, self.sim.mount_pitch = h, p

    # ============================================================ main loop
    def _loop(self):
        last_seq, last_src = 0, None
        while self.running:
            src = self.source
            if src is not last_src:
                last_seq, last_src = 0, src
            frame = src.read(timeout=0.25, after_seq=last_seq) if src else None
            now = time.monotonic()
            if frame is not None:
                last_seq = frame.seq
                try:
                    self._process(frame)
                except Exception as e:  # keep the device alive; report loudly
                    self.event("error", f"processing error: {e!r}")
                    time.sleep(0.1)
            else:
                self.sonifier.set_cues([])
                self.cues = []
            self._tick(now)

    def _process(self, frame):
        s = self.settings.s
        rng = self.cfg["ranges"][s.range]
        if self.recorder:
            self.recorder.write(frame)
        res = self.processor.process(frame, range_limit=rng, report_off_path=s.report_off_path,
                                     ear_height=self.cfg["audio"]["ear_height"])
        self.last_result = res
        self.health.on_frame(frame.t_capture, res.valid_ratio)
        self.m_frames.append(frame.t_capture)
        self.m_proc.append(res.timings["total"])

        cues = []
        if s.power and self.trainer.active:
            cues = self.trainer.cues()
        elif s.power and self.health.state in ("ok", "starting"):
            rep = res.reported
            if s.mode == "warning":
                rep = [o for o in rep if o["r"] <= s.warning_distance]
            for rank, o in enumerate(rep[: self.cfg["audio"]["max_cues"]]):
                cues.append({**o, "rank": rank, "t_frame": frame.t_capture})
        self.sonifier.range_limit = rng if not self.trainer.active else 4.0
        self.sonifier.set_cues(cues)
        self.cues = cues

        contacts = frame.extra.get("contacts", 0)
        if contacts > self._contacts_seen and self.sim:
            for c in self.sim.contacts[self._contacts_seen:contacts]:
                self.event("contact", f"Contact with {c['object']}", **c)
                self.logger.write("contact", **c)
                self.engine.schedule(earcon("contact", self.engine.sr), gain=0.6)
        self._contacts_seen = contacts

        if self.logger.active:
            self.logger.write("frame", seq=frame.seq, source=frame.source, valid=round(res.valid_ratio, 3),
                              ms=res.timings["total"],
                              detections=[{k: o[k] for k in ("id", "kind", "r", "az", "in_path")} for o in res.reported],
                              cues=[{k: c[k] for k in ("id", "kind", "r", "az", "rank")} for c in cues],
                              pose=frame.extra.get("pose"))

        if self.frame_listeners:
            now = time.monotonic()
            if now - self._last_packet >= 1.0 / self.cfg["web"]["viewer_fps"]:
                self._last_packet = now
                packet = self._packet(frame, res, cues)
                for fn in list(self.frame_listeners):
                    fn(packet)

    def _tick(self, now: float):
        ph = self.cfg["phrases"]
        st = self.health.tick(now)
        if st and self.settings.s.power:
            if st == "ok" and self.health.last_frame is not None:
                self.speech.say(ph["sensor_ok"], "confirm")
            elif st in ("blocked", "no_data"):
                self.speech.say(ph["sensor_" + st], "alarm")
                self.logger.write("alert", state=st)
            elif st == "repeat":
                self.speech.say(ph["sensor_" + self.health.state], "alarm")
        warn = self.battery.update()
        if warn and self.settings.s.power:
            self.speech.say(ph[warn].format(battery=self.battery.percent), "alarm")
            self.logger.write("alert", state=warn, battery=self.battery.percent)

    def _on_ping(self, cue: dict, onset: float, first: bool):
        if "t_frame" in cue:
            self.m_age.append((onset - cue["t_frame"]) * 1000)
        tid = cue.get("id")
        if first and tid not in self._pinged_tracks and cue.get("eligible_since"):
            self._pinged_tracks.add(tid)
            e2e = (onset - cue["eligible_since"]) * 1000
            if e2e < 2000:
                self.m_e2e.append(e2e)
                self.logger.write("cue_onset", id=tid, e2e_ms=round(e2e, 1), r=cue["r"], az=cue["az"])

    # ============================================================== metrics
    def metrics(self) -> dict:
        f = list(self.m_frames)
        fps = (len(f) - 1) / (f[-1] - f[0]) if len(f) > 2 and f[-1] > f[0] else 0.0

        def stat(d):
            v = list(d)
            if not v:
                return None
            v.sort()
            return {"mean": round(statistics.mean(v), 1), "p95": round(v[int(0.95 * (len(v) - 1))], 1),
                    "last": round(v[-1], 1) if v else None}

        return {"fps": round(fps, 1), "process_ms": stat(self.m_proc), "e2e_ms": stat(self.m_e2e),
                "data_age_ms": stat(self.m_age), "budget_ms": 300}

    def _packet(self, frame, res, cues) -> dict:
        vs = int(self.cfg["web"]["viewer_stride"])
        depth = frame.depth[::vs, ::vs]
        mm = np.clip(depth * 1000.0, 0, 65535).astype("<u2")
        intr = dict(frame.intrinsics)
        intr.update(stride=vs, w=mm.shape[1], h=mm.shape[0])
        src = self.source
        state = {
            "type": "frame", "seq": frame.seq, "source": frame.source, "source_label": getattr(src, "label", ""),
            "timings": res.timings, "valid_ratio": round(res.valid_ratio, 3), "ground": res.ground,
            "mount": {"height": self.processor.mount_height, "pitch": self.processor.mount_pitch},
            "intr": intr, "tracks": [t.to_dict(self.cfg["audio"]["ear_height"]) for t in res.tracks],
            "reported": [o["id"] for o in res.reported], "cues": [{k: c.get(k) for k in ("id", "kind", "r", "az", "el", "rank")} for c in cues],
            "pose": frame.extra.get("pose"), "movers": frame.extra.get("movers"), "contacts": frame.extra.get("contacts", 0),
            "scene_version": frame.extra.get("scene_version"),
            "health": self.health.state, "battery": {"percent": self.battery.percent, "charging": self.battery.charging},
            "settings": self.settings.to_dict(), "range_m": self.cfg["ranges"][self.settings.s.range],
            "metrics": self.metrics(),
            "audio": {"driver": getattr(self.driver, "name", ""), "peak_dbfs": round(self.engine.peak_dbfs, 1),
                      "limiting": self.engine.limiting, "pings": self.sonifier.pings, "underruns": self.engine.underruns,
                      "speech": self.speech.backend},
            "training": self.trainer.to_dict(), "logging": self.logger.active,
            "log_path": str(self.logger.path) if self.logger.active else None,
            "recording": self.recorder.path if self.recorder else None,
        }
        return {"state": state, "depth": mm}

    # ================================================================ events
    def event(self, kind: str, text: str, **data):
        ev = {"type": "event", "kind": kind, "text": text, "t": round(time.time(), 3), **data}
        self.events.append(ev)
        if kind in ("error", "warning", "source", "info"):
            print(f"[{kind}] {text}")
        for fn in list(self.event_listeners):
            fn(ev)

    # ========================================================= user actions
    def action(self, name: str, origin: str = "ui"):
        """Physical-control actions (power, volume, range, mode, battery, status, training, profile, logging)."""
        s, ph = self.settings.s, self.cfg["phrases"]
        if name != "power" and not s.power:
            self.engine.schedule(earcon("decline", self.engine.sr))
            return
        self.logger.write("control", action=name, origin=origin)
        if name == "power":
            self.settings.update(power=not s.power)
            if s.power:
                self.sonifier.enabled = True
                threading.Thread(target=self._self_test, daemon=True).start()
            else:
                self.sonifier.enabled = False
                self.trainer.stop()
                self.speech.say(ph["power_off"], "power_off")
        elif name in ("volume_up", "volume_down"):
            nv = s.volume + (1 if name == "volume_up" else -1)
            if 0 <= nv <= 10:
                self.settings.update(volume=nv)
                self.engine.master_gain = volume_gain(nv)
                self.speech.say(ph["volume"].format(volume=nv), "confirm")
            else:
                self.engine.schedule(earcon("limit", self.engine.sr))
        elif name == "range_next":
            nr = RANGES[(RANGES.index(s.range) + 1) % len(RANGES)]
            self.settings.update(range=nr)
            self.speech.say(ph["range"].format(range=nr, meters=self.cfg["ranges"][nr]), "confirm")
        elif name == "mode_next":
            nm = MODES[(MODES.index(s.mode) + 1) % len(MODES)]
            self.settings.update(mode=nm)
            self.speech.say(ph["mode_" + nm], "confirm")
        elif name == "battery":
            self.speech.say(ph["battery"].format(battery=self.battery.percent), "confirm")
        elif name == "status":
            sensor = {"ok": "Sensor OK", "blocked": "Sensor blocked", "no_data": "Sensor not responding"}.get(
                self.health.state, "Sensor starting")
            self.speech.say(ph["status"].format(sensor=sensor, range=s.range, mode=s.mode.capitalize(),
                                                battery=self.battery.percent), "confirm")
        elif name == "training":
            if self.trainer.active:
                self.trainer.stop()
                self.speech.say(ph["training_off"], "confirm")
            else:
                self.trainer.start()
                self.speech.say(ph["training_on"], "confirm")
            self.logger.write("training", active=self.trainer.active)
        elif name == "profile_next":
            np_ = PROFILES[(PROFILES.index(s.profile) + 1) % len(PROFILES)] if s.profile in PROFILES else PROFILES[0]
            self.settings.update(profile=np_)
            self.sonifier.profile = np_
            self.speech.say(ph["profile"].format(profile=np_), "confirm")
        elif name == "logging":
            if self.logger.active:
                self.logger.stop()
                self.settings.update(logging=False)
                self.event("info", "Session logging stopped")
            else:
                self.settings.update(logging=True)
                self._start_log()
        else:
            self.event("warning", f"unknown action {name}")
            return
        self.event("action", name, origin=origin)

    def _start_log(self):
        self.logger.start({"settings": self.settings.to_dict(), "source": getattr(self.source, "kind", None),
                           "profile": self.cfg.get("device_profile"), "config": {k: self.cfg[k] for k in ("sensor", "processing", "ranges", "audio")}})
        self.event("info", f"Session logging to {self.logger.path}")

    def _self_test(self):
        """Start-up confirmation and self-test announcement (proposal A2)."""
        ph, s = self.cfg["phrases"], self.settings.s
        self.speech.say(ph["startup"], "power_on")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 4.0 and self.health.state != "ok":
            time.sleep(0.1)
        if self.health.state == "ok":
            self.speech.say(ph["ready"].format(battery=self.battery.percent, range=s.range, mode=s.mode.capitalize()))
        else:
            self.speech.say(ph["sensor_no_data"], "alarm")

    # ================================================== console commands
    def set_setting(self, key: str, value):
        if key in ("report_off_path",):
            self.settings.update(**{key: bool(value)})
        elif key == "warning_distance":
            self.settings.update(warning_distance=float(value))
        elif key in ("mount_height", "mount_pitch"):
            self.settings.update(**{key: None if value is None else float(value)})
            self._apply_mount()
        elif key == "calibrate_mount":
            g = self.processor.ground_info()
            if g["ok"]:
                self.settings.update(mount_height=g["height"], mount_pitch=g["pitch"])
                self._apply_mount()
                self.event("info", f"Mount calibrated from the floor: {g['height']:.2f} m, {g['pitch']:.1f} deg")
            else:
                self.event("warning", "Calibration needs a clear view of the floor")
        elif key == "flip_horizontal" and hasattr(self.source, "flip"):
            self.source.flip = bool(value)
            intr = self.source.intrinsics
            intr["cx"] = intr["width"] - 1 - intr["cx"]
            self.cfg["sensor"]["flip_horizontal"] = bool(value)
        elif key == "battery":
            self.battery.set(float(value))
        elif key == "charging":
            self.battery.charging = bool(value)
        else:
            self.event("warning", f"unknown setting {key}")

    def sim_command(self, key: str, value):
        sim = self.sim
        if not sim:
            self.event("warning", "Simulator controls need the simulator source")
            return
        if key == "scene":
            self.scene = value
            sim.load_scene(value)
            self.processor.reset()
            self._contacts_seen = 0
            self.event("scene", f"Scene: {sim.world.scene.get('name')}", scene=sim.world.to_dict())
        elif key == "reset":
            sim.reset_pose()
            self.processor.reset()
            self._contacts_seen = 0
        elif key == "move":
            sim.move = {"f": float(value.get("f", 0)), "s": float(value.get("s", 0)), "turn": float(value.get("turn", 0))}
        elif key == "sun":
            sim.sun = max(0.0, min(1.0, float(value)))
        elif key in ("blocked", "head_bob", "autowalk"):
            setattr(sim, key, bool(value))
        elif key == "teleport":
            sim.set_pose(value["x"], value["z"], value.get("yaw", math.degrees(sim.yaw)))
        else:
            self.event("warning", f"unknown sim command {key}")

    def training_command(self, msg: dict):
        cmd = msg.get("cmd")
        if cmd == "start" and not self.trainer.active:
            self.action("training")
        elif cmd == "stop" and self.trainer.active:
            self.action("training")
        elif cmd == "target":
            self.trainer.set_target(az=msg.get("az"), r=msg.get("r"), kind=msg.get("kind"), el=msg.get("el"))
        elif cmd == "random":
            self.trainer.random_target()
            self.event("info", "Blind trial: point to the sound")
        elif cmd == "respond":
            tr = self.trainer.respond(msg["az"])
            self.logger.write("training_trial", **tr)
            self.event("training", f"Target {tr['target_az']:+.0f} deg, answered {tr['response_az']:+.0f} deg, "
                                   f"error {tr['error_deg']:.0f} deg", trial=tr)

    def hello(self) -> dict:
        sim = self.sim
        return {"type": "hello", "version": 1, "profile": self.cfg.get("device_profile"),
                "sensor": self.cfg["sensor"], "processing": self.cfg["processing"], "ranges": self.cfg["ranges"],
                "sources": available_sources(self.cfg), "source": getattr(self.source, "kind", None),
                "scene": sim.world.to_dict() if sim else None, "scenes": list_scenes(),
                "settings": self.settings.to_dict(), "profiles": list(PROFILES), "events": list(self.events)[-30:],
                "sim": sim.info() if sim else None}
