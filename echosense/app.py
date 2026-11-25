"""Echo-Sense device application: sensing -> processing -> audio, plus power & control.

This is the program that runs on the Orange Pi. On a laptop it runs the same
way against the simulator; in Docker it runs inside the virtual Orange Pi.
"""
from __future__ import annotations

import json
import math
import statistics
import struct
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
from .device.perf import PerfMonitor
from .device.settings import MODES, RANGES, SENSITIVITIES, SettingsStore
from .device.training import Trainer
from .processing.objects import SPOKEN
from .processing.pipeline import DepthProcessor
from .sim.world import list_scenes
from .sensors.presets import PRESETS, coverage
from .sensors import NoSensor, Recorder, SimulatedKinect, SourceError, available_sources, open_source


def volume_gain(v: int) -> float:
    return 0.0 if v <= 0 else 10 ** (((v - 10) * 2.5) / 20)


class EchoSense:
    def __init__(self, cfg: dict, source: str = "auto", scene: str = "maysan_street", audio: str | None = None,
                 record: str | None = None, settings_path: str | None = None, perf_csv: str | None = None):
        self.cfg = cfg
        self.scene = scene
        self.source_request = source
        self.audio_kind = audio or cfg["audio"]["driver"]
        self.record_path = record
        self.settings = SettingsStore(expand(settings_path or cfg["paths"]["settings"]))
        s = self.settings.s
        self._overhang_default = cfg["processing"]["overhang_min_height"]
        if s.head_height is not None:
            cfg["processing"]["user_height"] = s.head_height
            cfg["processing"]["overhang_min_height"] = min(self._overhang_default,
                max(0.2, (s.ear_height or s.head_height) - cfg["processing"]["head_margin"]))
        a = cfg["audio"]
        if s.ear_height is not None:
            a["ear_height"] = s.ear_height
        self.engine = AudioEngine(a["sample_rate"], a["block_size"], a["max_output_dbfs"])
        self.engine.master_gain = volume_gain(s.volume)
        self.hrtf = StructuralHRTF(a["sample_rate"], a["head_radius"])
        self.sonifier = Sonifier(a["sample_rate"], self.hrtf, cfg["ranges"][s.range], s.profile, on_ping=self._on_ping)
        self.engine.generators.append(self.sonifier)
        self.browser_tap = BrowserTap(self.engine)
        self.events: deque = deque(maxlen=200)
        self.event_listeners: list = []
        self.frame_listeners: list = []
        self.rgb_listeners: list = []  # callables(bytes): colour frames for the console
        self.rgb_viewers = 0
        cfg["web"].setdefault("viewer_stride_default", cfg["web"]["viewer_stride"])
        self._last_rgb_t = 0.0
        self._last_rgb_sent = 0.0
        self.speech = Speech(self.engine, cfg["speech"], on_say=lambda txt: self.event("speech", txt))
        self.health = HealthMonitor(cfg["health"])
        self.battery = Battery(cfg["battery"])
        self.logger = SessionLogger(expand(cfg["paths"]["logs"]))
        self.trainer = Trainer()
        self.processor = DepthProcessor(cfg)
        self.processor.set_sensitivity(self.settings.s.sensitivity)
        self.source = None
        self.driver = None
        self.gpio = None
        self.recorder = None
        self.running = False
        self._source_lock = threading.Lock()
        self._hw_target = None  # hardware source to keep alive ("kinect1", "kinect2" or "auto")
        self._reconnecting = False
        self._last_reconnect = 0.0
        self._outage_since = None
        self._last_packet = 0.0
        self._contacts_seen = 0
        self._pinged_tracks: set = set()
        self._announced: dict = {}
        self._last_announce = 0.0
        self._label_said: dict = {}  # label -> (time, direction) of the last automatic announcement
        self._floor_noted = False
        self._pointing = None  # "floor" | "sky" while the accelerometer says the sensor points there
        self._pointing_since = 0.0
        self.m_frames: deque = deque(maxlen=60)
        self.m_proc: deque = deque(maxlen=120)
        self.m_e2e: deque = deque(maxlen=50)
        self.m_age: deque = deque(maxlen=120)
        self.cues: list = []
        self.last_result = None
        self.perf_processed = 0  # counters read by the performance monitor
        self.perf_dropped = 0
        self.perf_busy_s = 0.0
        self._map_reset = False  # set by the console, done on the processing thread
        self._world_snap = None
        self._world_snap_t = 0.0
        self._apply_mount()
        self.perf = PerfMonitor(self, csv_path=perf_csv)

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
        self.perf.start()
        if self.settings.s.power:
            threading.Thread(target=self._self_test, name="self-test", daemon=True).start()

    def stop(self):
        self._hw_target = None  # shutting down: nothing to reconnect
        self.running = False
        self.perf.stop()
        if self.source:
            self.source.stop()
        if self.driver:
            self.driver.stop()
        if self.recorder:
            self.recorder.close()
        self.logger.stop()

    # ============================================================== sources
    def switch_source(self, name: str, reconnect: bool = False) -> bool:
        hardware = name == "auto" or name.startswith("kinect")
        if not reconnect:
            self._hw_target = name if hardware else None
        with self._source_lock:
            old = self.source
            same = old is not None and (old.kind == name or name.startswith("replay:") and old.kind == "replay")
            if same:
                old.stop()  # a device can only be opened once
            try:
                src = open_source(name, self.cfg, self.scene)
            except SourceError as e:
                if reconnect:
                    if same:  # the device is still gone: keep searching, never substitute fake depth
                        self.source = NoSensor(name, str(e))
                        self.source.start()
                    return False
                self.event("error", f"Could not open {name}: {e}")
                if old is not None and not same and old.kind != "none":
                    return False  # keep the current source running
                if hardware and not self.cfg.get("allow_sim_fallback", True):
                    src = NoSensor(name, str(e))
                    src.start()
                else:
                    src = open_source("sim", self.cfg, self.scene)
            if old is not None and not same and old is not src:
                old.stop()
            self.source = src
            if src.error:
                self.event("warning", src.error)
            self.processor.reset()
            self.health.reset()
            self._floor_noted = False
            self._pinged_tracks.clear()
            self._contacts_seen = 0
            self._apply_mount()
            self.processor.near_mode = bool(self.settings.s.near_mode)
            src.set_rgb(self.rgb_viewers > 0)
            if self.settings.s.near_mode and hasattr(src, "set_near_mode"):
                src.set_near_mode(True)
            if self.record_path and src is not old:
                if self.recorder:
                    self.recorder.close()
                self.recorder = Recorder(self.record_path, src.intrinsics, src.kind)
                self.event("info", f"Recording depth to {self.record_path}")
            self.event("source", f"Depth source: {src.label}", source=src.kind)
            self.logger.write("source", source=src.kind)
            return src.kind == name or name == "auto"

    def helper_pids(self) -> dict:
        """Child processes and what they stand for, so their CPU is counted in the right budget."""
        src, out = self.source, {}
        proc = getattr(src, "_proc", None)  # simulator render worker
        if proc is not None and getattr(proc, "pid", None):
            out[proc.pid] = "simulator"
        bridge = getattr(src, "proc", None)  # echosense-kinect2 (libfreenect2)
        if bridge is not None and getattr(bridge, "pid", None):
            out[bridge.pid] = "driver"
        return out

    @property
    def sim(self) -> SimulatedKinect | None:
        return self.source if isinstance(self.source, SimulatedKinect) else None

    def _apply_near_mode(self):
        on = bool(self.settings.s.near_mode)
        self.processor.near_mode = on
        src = self.source
        if hasattr(src, "set_near_mode"):
            src.set_near_mode(on)

            def report():
                for _ in range(20):
                    time.sleep(0.1)
                    if src.near_mode_result is not None:
                        break
                if not on:
                    self.event("info", "Near mode off")
                elif src.near_mode_result:
                    self.event("info", "Near mode on: Kinect hardware near mode (0.4-3 m) + close-range memory")
                else:
                    self.event("info", "This Kinect has no hardware near mode (Xbox 360 model). Software near mode on: "
                                       "obstacles that come closer than the sensor can see keep sounding for 1.5 s.")
            threading.Thread(target=report, daemon=True).start()
        elif on:
            self.event("info", "Near mode on: close-range memory (this sensor has no hardware near mode)")
        else:
            self.event("info", "Near mode off")

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
                if last_seq and frame.seq > last_seq + 1:
                    self.perf_dropped += frame.seq - last_seq - 1  # sensor produced frames we never processed
                last_seq = frame.seq
                t0 = time.perf_counter()
                try:
                    self._process(frame)
                except Exception as e:  # keep the device alive; report loudly
                    self.event("error", f"processing error: {e!r}")
                    time.sleep(0.1)
                self.perf_busy_s += time.perf_counter() - t0
                self.perf_processed += 1
            else:
                self.sonifier.set_cues([])
                self.cues = []
            self._tick(now)

    def _process(self, frame):
        s = self.settings.s
        rng = self.cfg["ranges"][s.range]
        if self.recorder:
            self.recorder.write(frame)
        if self._map_reset and self.processor.mapper is not None:
            self._map_reset = False
            self.processor.mapper.reset()
        res = self.processor.process(frame, range_limit=rng, report_off_path=s.report_off_path,
                                     ear_height=self.cfg["audio"]["ear_height"])
        self.last_result = res
        self.health.on_frame(frame.t_capture, res.valid_ratio, res.quality)
        self.m_frames.append(frame.t_capture)
        self.m_proc.append(res.timings["total"])

        cues = []
        if s.power and self.trainer.active:
            cues = self.trainer.cues()
        elif s.power and self.health.state in ("ok", "starting", "degraded"):
            rep = res.reported
            if s.mode == "warning":
                rep = [o for o in rep if o["r"] <= s.warning_distance]
            max_cues = min(self.cfg["audio"]["max_cues"], int(self.processor.sens.get("max_cues", 99)))
            for rank, o in enumerate(rep[:max_cues]):
                cues.append({**o, "rank": rank, "t_frame": frame.t_capture})
        if s.power and s.announce and not self.trainer.active and self.health.state != "blocked":
            self._auto_announce(res.reported, frame.t_capture)
        self.sonifier.range_limit = rng if not self.trainer.active else 4.0
        self.sonifier.set_cues(cues)
        self.cues = cues

        self._check_pointing(frame.t_capture)
        if self.processor.plane_auto and not self._floor_noted:
            self._floor_noted = True
            g = res.ground
            self.event("warning", f"The floor is not where the mount settings say: the sensor looks about "
                                  f"{g['height']:.2f} m high, tilted {g['pitch']:.0f} deg. Following it automatically; "
                                  f"press \"Calibrate from floor\" to save it.")

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

        if self.rgb_viewers and self.rgb_listeners:
            self._send_rgb()
        if self.frame_listeners:
            now = time.monotonic()
            if now - self._last_packet >= 1.0 / self.cfg["web"]["viewer_fps"]:
                self._last_packet = now
                packet = self._packet(frame, res, cues)
                for fn in list(self.frame_listeners):
                    fn(packet)

    def _watchdog(self, now: float):
        """Kinects can drop off USB for a moment (a hub hiccup, a loose cable, a brown-out): libfreenect
        then marks the camera dead and stops streaming. Reopen until the stream is back. When the
        Kinect was asked for but isn't there (device: "no sensor"; laptop: simulator stand-in), keep
        looking for it so plugging it in is enough."""
        if self._hw_target is None or self._reconnecting:
            return
        src = self.source
        kind = getattr(src, "kind", None)
        rc = self.cfg.get("reconnect", {})
        retry = rc.get("retry_s", 2.0)
        if kind in ("kinect1", "kinect2"):
            last = self.health.last_frame
            stalled = (not src.running or (last is None and self._outage_since is not None)
                       or (last is not None and now - last > rc.get("stall_s", 1.5)))
            if not stalled:
                if self._outage_since is not None and last is not None:
                    self.event("info", f"Depth sensor reconnected after {now - self._outage_since:.1f} s")
                    self.logger.write("reconnected", outage_s=round(now - self._outage_since, 2))
                    self._outage_since = None
                return
            if self._outage_since is None:
                self._outage_since = now
                self.event("warning", "Depth sensor stopped streaming; reconnecting"
                           + (f" ({src.error})" if src.error else ""))
                self.logger.write("disconnected", source=kind)
            target = self._hw_target if self._hw_target != "auto" else kind
        else:
            if kind == "none" and self._outage_since is None:
                self._outage_since = now
            retry = retry if kind == "none" else 5.0
            target = self._hw_target
        if now - self._last_reconnect < retry:
            return
        self._reconnecting, self._last_reconnect = True, now

        def run():
            try:
                self.switch_source(target, reconnect=True)
            finally:
                self._last_reconnect = time.monotonic()
                self._reconnecting = False

        threading.Thread(target=run, name="reconnect", daemon=True).start()

    def _tick(self, now: float):
        self._watchdog(now)
        ph = self.cfg["phrases"]
        st = self.health.tick(now)
        if st and self.settings.s.power:
            if st == "ok" and self.health.last_frame is not None:
                self.speech.say(ph["sensor_ok"], "confirm")
            elif st in ("blocked", "no_data"):
                self.speech.say(ph["sensor_" + st], "alarm")
                self.logger.write("alert", state=st)
            elif st == "degraded":
                self.speech.say(ph["sensor_degraded"], "limit")
                self.logger.write("alert", state=st, quality=round(self.health.quality, 2))
            elif st == "repeat":
                self.speech.say(ph["sensor_" + self.health.state], "alarm")
        warn = self.battery.update()
        if warn and self.settings.s.power:
            self.speech.say(ph[warn].format(battery=self.battery.percent), "alarm")
            self.logger.write("alert", state=warn, battery=self.battery.percent)

    def _check_pointing(self, t: float):
        """With a verified accelerometer: say so when the sensor points at the floor or the sky for a
        second (strap slipped, device knocked or set down), once per episode."""
        ang = self.processor.gravity.angles(t)
        where = None
        if ang is not None:
            if ang["pitch"] < self.cfg["sensor"].get("pointing_floor_deg", -45.0):
                where = "floor"
            elif ang["pitch"] > self.cfg["sensor"].get("pointing_sky_deg", 25.0):
                where = "sky"
        if where != self._pointing:
            self._pointing, self._pointing_since, self._pointing_said = where, t, False
        elif where and not getattr(self, "_pointing_said", False) and t - self._pointing_since >= 1.0:
            self._pointing_said = True
            text = self.cfg["phrases"]["pointing_" + where]
            self.event("warning", text)
            self.logger.write("alert", state="pointing_" + where, pitch=ang["pitch"])
            if self.settings.s.power:
                self.speech.say(text, "limit")

    def gravity_state(self) -> dict:
        g = self.processor.gravity
        t = self.last_result.t_capture if self.last_result is not None else 0.0
        return {"status": g.status(), "device": getattr(self.source, "accel_status", None), "angles": g.angles(t)}

    # ------------------------------------------------------- what's ahead
    def _direction(self, az: float) -> str:
        ph = self.cfg["phrases"]
        if abs(az) < 8:
            return ph["dir_ahead"]
        if abs(az) < 22:
            return ph["dir_slight_right"] if az > 0 else ph["dir_slight_left"]
        return ph["dir_right"] if az > 0 else ph["dir_left"]

    def describe_scene(self) -> str:
        """Spoken summary of what is ahead (the "What's ahead?" button): up to three obstacles,
        in-path first, nearest first, with their recognised type."""
        ph, s = self.cfg["phrases"], self.settings.s
        rng = self.cfg["ranges"][s.range]
        res = self.last_result
        items = []
        if res is not None:
            cands = [t for t in res.tracks if t.confirmed and not t.suppressed and t.r <= max(rng, 2.5)]
            cands.sort(key=lambda t: (0 if t.in_path else 1, t.r))
            for t in cands[:3]:
                m = round(t.r * 2) / 2
                item = ph["describe_item"].format(label=SPOKEN.get(t.label, "Obstacle"), meters=f"{m:g}",
                                                  direction=self._direction(t.az))
                items.append(item.replace(" 1 meters", " 1 meter"))
        return ". ".join(items) + "." if items else ph["describe_none"].format(range=rng)

    def _auto_announce(self, reported: list, t: float):
        """Say "Person ahead" / "Vehicle left" when one appears. Only for something seen steadily
        (about a quarter of a second, label settled), at most once per track, and the same thing in
        the same direction is not repeated for 8 s even when the tracker loses and re-finds it."""
        ph = self.cfg["phrases"]
        for o in reported:
            if (o["label"] not in ("person", "vehicle") or o["id"] in self._announced
                    or o["label_conf"] < 0.6 or o.get("hits", 99) < 8):
                continue
            self._announced[o["id"]] = t
            d = self._direction(o["az"])
            last = self._label_said.get(o["label"])
            if last is not None and t - last[0] < 8.0 and last[1] == d:
                continue
            if t - self._last_announce > 3.0:
                self._last_announce = t
                self._label_said[o["label"]] = (t, d)
                self.speech.say(ph["announce"].format(label=SPOKEN[o["label"]], direction=d))
                break
        if len(self._announced) > 200:
            self._announced = dict(list(self._announced.items())[-100:])

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
            "quality": round(self.health.quality, 3), "coverage": self.coverage(),
            "flip": getattr(src, "flip", None), "has_rgb": bool(getattr(src, "has_rgb", False)),
            "depth_stats": self.processor.depth_stats, "gravity": self.gravity_state(),
            "mount": {"height": self.processor.mount_height, "pitch": self.processor.mount_pitch},
            "intr": intr, "tracks": [t.to_dict(self.cfg["audio"]["ear_height"]) for t in res.tracks],
            "reported": [o["id"] for o in res.reported], "cues": [{k: c.get(k) for k in ("id", "kind", "r", "az", "el", "rank", "ttc", "r_urgent", "label", "label_conf", "memory")} for c in cues],
            "recognition": self.processor.recognizer is not None and self.processor.recognizer.available,
            "pose": frame.extra.get("pose"), "movers": frame.extra.get("movers"), "contacts": frame.extra.get("contacts", 0),
            "scene_version": frame.extra.get("scene_version"), "world": self._world_state(),
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

    def _world_state(self):
        """The world map for the console: the map image about twice a second (it's the bulk of the
        data), pose and status every packet."""
        m = self.processor.mapper
        if m is None:
            return None
        now = time.monotonic()
        if self._world_snap is None or now - self._world_snap_t >= 0.5:
            self._world_snap, self._world_snap_t = m.snapshot(), now
            return self._world_snap
        return {**m.state(), "trail": m.trail[-300:], "truth": m.truth_trail[-300:] or None}

    def map_command(self, cmd: str):
        if cmd == "reset":
            self._map_reset = True
            self._world_snap = None
            self.event("info", "World map cleared")

    # ============================================================== colour
    def set_rgb_viewers(self, n: int):
        """The colour stream runs only while a console is showing it (it costs USB bandwidth and CPU)."""
        self.rgb_viewers = max(0, n)
        if self.source:
            self.source.set_rgb(self.rgb_viewers > 0)

    def _send_rgb(self):
        now = time.monotonic()
        if now - self._last_rgb_sent < 1.0 / self.cfg["web"].get("rgb_fps", 8):
            return
        got = self.source.rgb() if self.source else None
        if got is None or got[2] == self._last_rgb_t:
            return
        img, intr, t = got
        self._last_rgb_t, self._last_rgb_sent = t, now
        s = int(self.cfg["web"].get("rgb_stride", 2))
        small = np.ascontiguousarray(img[::s, ::s])
        meta = {"w": small.shape[1], "h": small.shape[0], "stride": s, "intr": intr}
        js = json.dumps(meta, separators=(",", ":")).encode()
        js += b" " * ((-len(js)) % 4)
        data = b"\x03\x00\x00\x00" + struct.pack("<I", len(js)) + js + small.tobytes()
        for fn in list(self.rgb_listeners):
            fn(data)

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
        elif name == "describe":
            text = self.describe_scene()
            self.speech.say(text)
            self.logger.write("describe", text=text)
        elif name == "status":
            sensor = {"ok": "Sensor OK", "blocked": "Sensor blocked", "no_data": "Sensor not responding",
                      "degraded": "Depth sensing degraded"}.get(
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
        if key == "near_mode":
            self.settings.update(near_mode=bool(value))
            self._apply_near_mode()
        elif key in ("report_off_path", "announce"):
            self.settings.update(**{key: bool(value)})
        elif key == "sensitivity":
            if value not in SENSITIVITIES:
                self.event("warning", f"unknown sensitivity {value}")
                return
            self.settings.update(sensitivity=value)
            self.processor.set_sensitivity(value)
            self.event("info", f"Sensitivity {value}")
        elif key == "warning_distance":
            self.settings.update(warning_distance=float(value))
        elif key in ("mount_height", "mount_pitch"):
            self.settings.update(**{key: None if value is None else float(value)}, fitting_checked=False)
            self._apply_mount()
        elif key == "calibrate_mount":
            g = self.processor.calibrate_floor()
            if g:
                self.settings.update(mount_height=g["height"], mount_pitch=g["pitch"], fitting_checked=False)
                self._apply_mount()
                self.processor.reset()
                self._floor_noted = False
                self.event("info", f"Mount calibrated from the floor: {g['height']:.2f} m, {g['pitch']:.1f} deg")
            else:
                self.event("warning", "Calibration needs a clear view of the floor")
        elif key == "flip_horizontal" and hasattr(self.source, "flip"):
            if bool(value) != self.source.flip:
                self.source.flip = bool(value)
                intr = self.source.intrinsics
                intr["cx"] = intr["width"] - 1 - intr["cx"]
            self.cfg["sensor"]["kinect1_flip" if self.source.kind == "kinect1" else "flip_horizontal"] = bool(value)
            self.processor.reset()
        elif key == "battery":
            self.battery.set(float(value))
        elif key == "charging":
            self.battery.charging = bool(value)
        else:
            self.event("warning", f"unknown setting {key}")

    def sim_command(self, key: str, value):
        sim = self.sim
        if not sim:
            if key != "move":  # walking keys mean nothing for a real sensor: ignore them quietly
                self.event("warning", "Simulator controls need the simulator source")
            return
        if key == "scene":
            self.scene = value
            sim.load_scene(value)
            self.processor.reset()
            self._contacts_seen = 0
            self.event("scene", f"Scene: {sim.world.scene.get('name')}", scene=sim.world.to_dict())
        elif key == "sensor_preset":
            if value not in PRESETS:
                self.event("warning", f"unknown sensor preset {value}")
                return
            self.cfg.setdefault("sim", {})["sensor_preset"] = value
            pose = (sim.x, sim.z, math.degrees(sim.yaw))
            self.switch_source("sim")
            if self.sim:
                self.sim.set_pose(*pose)
            self.event("info", f"Simulated sensor: {PRESETS[value]['label']}")
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
            self.logger.write("training_trial", **{
                ("obstacle_kind" if key == "kind" else key): value for key, value in tr.items()
            })
            self.event("training", f"Target {tr['target_az']:+.0f} deg, answered {tr['response_az']:+.0f} deg, "
                                   f"error {tr['error_deg']:.0f} deg", trial=tr)
            return tr

    def configure_fitting(self, values: dict):
        from .device.supervision import number
        head = number(values.get("head_height"), 0.4, 2.3)
        ear = number(values.get("ear_height"), 0.3, head)
        mount = number(values.get("mount_height"), 0.2, ear)
        pitch = number(values.get("mount_pitch"), -40, 20)
        if values.get("fitting_checked") is not True:
            raise ValueError("Check the sensor fitting and blind zones before saving.")
        self.settings.update(head_height=head, ear_height=ear, mount_height=mount,
                             mount_pitch=pitch, fitting_checked=True)
        self.cfg["audio"]["ear_height"] = ear
        self.cfg["processing"]["user_height"] = head
        self.cfg["processing"]["overhang_min_height"] = min(
            self._overhang_default, max(0.2, ear - self.cfg["processing"]["head_margin"]))
        self._apply_mount()
        self.event("info", "Child fitting measurements saved. Check coverage before practice.")

    def sensor_spec(self) -> dict:
        return self.source.spec(self.cfg) if self.source else dict(self.cfg["sensor"])

    def coverage(self) -> dict:
        p = self.cfg["processing"]
        return coverage(self.sensor_spec(), self.processor.mount_height, self.processor.mount_pitch,
                        head_height=self.settings.s.head_height or 1.7, corridor_half=p["corridor_half_width"])

    def hello(self) -> dict:
        sim = self.sim
        return {"type": "hello", "version": 1, "profile": self.cfg.get("device_profile"),
                "sensor": self.sensor_spec(), "processing": self.cfg["processing"], "ranges": self.cfg["ranges"],
                "sensor_presets": {k: v["label"] for k, v in PRESETS.items()},
                "sensor_preset": self.cfg.get("sim", {}).get("sensor_preset", "kinect_v2"),
                "sources": available_sources(self.cfg), "source": getattr(self.source, "kind", None),
                "scene": sim.world.to_dict() if sim else None, "scenes": list_scenes(),
                "settings": self.settings.to_dict(), "profiles": list(PROFILES), "events": list(self.events)[-30:],
                "sim": sim.info() if sim else None,
                "perf": {"host": self.perf.host, "history": list(self.perf.history)[-120:]}}
