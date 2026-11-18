"""Simulated Kinect v2 walking through a virtual scene."""
from __future__ import annotations

import math
import multiprocessing as mp
import threading
import time

import numpy as np

from ..sim.world import CameraModel, World, camera_rotation
from .base import DepthSource
from .presets import PRESETS, SPEC_KEYS


class SimulatedKinect(DepthSource):
    kind = "sim"
    label = "Simulated Kinect v2"

    WALK_SPEED = 1.0  # m/s
    TURN_SPEED = math.radians(75)  # rad/s
    BODY_RADIUS = 0.25
    SENSOR_FORWARD = 0.12  # chest-mounted sensor sits in front of the body centre

    def __init__(self, cfg: dict, scene: str = "maysan_street", seed: int | None = None):
        super().__init__()
        self.cfg = cfg
        s = cfg["sensor"]
        self.preset = cfg.get("sim", {}).get("sensor_preset", "kinect_v2")
        self.sensor = {**s, **PRESETS.get(self.preset, PRESETS["kinect_v2"])}
        self.label = f"Simulated {self.sensor['label'].split(' (')[0]}"
        self.cam = CameraModel.from_config(self.sensor)
        self.intrinsics = {k: self.sensor[k] for k in ("width", "height", "fx", "fy", "cx", "cy")}
        # a lite profile may ask for fewer frames than the sensor can give (e.g. 15 on an Orange Pi One)
        self.fps = float(min(self.sensor.get("fps", 30), s.get("fps", 30)))
        self.rng = np.random.default_rng(seed)
        self.mount_height = float(s["mount_height"])
        self.mount_pitch = float(s["mount_pitch_deg"])
        # interactive state (written by the UI thread, read by the sim thread)
        self.move = {"f": 0.0, "s": 0.0, "turn": 0.0}
        self.sun = 0.0
        self.blocked = False
        self.head_bob = True
        self.autowalk = False
        self.want_rgb = False
        self._rgb = None
        self.contacts: list[dict] = []
        self._contact_last: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._scene_lock = threading.Lock()
        self.load_scene(scene)

    # ---------------------------------------------------------------- scene
    def load_scene(self, scene: str | dict):
        with self._scene_lock:
            self.world = World(scene)
            st = self.world.scene.get("start", {})
            self.x, self.z = float(st.get("x", 0.0)), float(st.get("z", 0.0))
            self.yaw = math.radians(float(st.get("yaw", 0.0)))
            self.route = [tuple(p) for p in self.world.scene.get("route", [])]
            self.route_i = 1
            self.step_phase = 0.0
            self.contacts = []
            self._contact_last = {}
            self.scene_version = getattr(self, "scene_version", 0) + 1

    def reset_pose(self):
        self.load_scene(self.world.scene)

    def set_pose(self, x: float, z: float, yaw_deg: float):
        self.x, self.z, self.yaw = float(x), float(z), math.radians(yaw_deg)

    # ----------------------------------------------------------------- loop
    # Live mode renders in a separate worker process, like the real Kinect v2 whose depth decoding
    # runs in the echosense-kinect2 process. The sensor then never competes with the device
    # pipeline for the Python interpreter, so latency measured in the simulator (and in the virtual
    # Orange Pi) reflects the device software, not the simulator.
    def start(self, use_process: bool = True):
        super().start()
        self._conn = self._proc = None
        if use_process:
            ctx = mp.get_context("spawn")
            self._conn, child = ctx.Pipe()
            self._proc = ctx.Process(target=_render_worker, args=(child, self.sensor, int(self.rng.integers(1 << 30))),
                                     name="sim-kinect-render", daemon=True)
            self._proc.start()
        self._thread = threading.Thread(target=self._run, name="sim-kinect", daemon=True)
        self._thread.start()

    def stop(self):
        super().stop()
        if self._thread:
            self._thread.join(timeout=2)
        if getattr(self, "_proc", None):
            try:
                self._conn.send(None)
            except (BrokenPipeError, OSError):
                pass
            self._proc.join(timeout=2)
            if self._proc.is_alive():
                self._proc.terminate()

    def _run(self):
        period = 1.0 / self.fps
        t0 = time.monotonic()
        last = t0
        next_t = t0
        worker_scene = None
        while self.running:
            now = time.monotonic()
            if now < next_t:
                time.sleep(next_t - now)
                now = time.monotonic()
            next_t = max(next_t + period, now)
            dt = min(now - last, 0.2)
            last = now
            t = now - t0
            with self._scene_lock:
                self._step(dt, t)
                o, R, pitch = self.sensor_pose()
                extra = self._extra(o, pitch)
                scene = self.world.scene
                sun, blocked = self.sun, self.blocked
                want_rgb = self.want_rgb
                rgb = None
                if self._conn is None:
                    out = self.world.render(o, R, self.cam, self.rng, sun=sun, blocked=blocked, want_rgb=want_rgb)
                    depth, rgb = out if want_rgb else (out, None)
            if self._conn is not None:
                try:
                    if worker_scene is not scene:
                        self._conn.send(("scene", scene))
                        worker_scene = scene
                    self._conn.send(("render", t, o, R, sun, blocked, want_rgb))
                    buf = self._conn.recv_bytes()
                    if want_rgb:
                        rgb = np.frombuffer(self._conn.recv_bytes(), np.uint8).reshape(self.cam.height, self.cam.width, 3)
                except (EOFError, BrokenPipeError, OSError):
                    self.error = "simulator render process stopped"
                    break
                depth = np.frombuffer(buf, np.float32).reshape(self.cam.height, self.cam.width)
            # stamped on arrival, exactly like frames from the Kinect bridge
            now = time.monotonic()
            if rgb is not None:
                self._rgb = (rgb, now)
            self._publish(depth, now, **extra)

    def _step(self, dt: float, t: float):
        f, s, turn = self.move["f"], self.move["s"], self.move["turn"]
        if self.autowalk and self.route:
            f, s, turn = self._autowalk_controls()
        self.yaw += turn * self.TURN_SPEED * dt
        moving = abs(f) > 1e-3 or abs(s) > 1e-3
        if moving:
            speed = self.WALK_SPEED * (0.8 if self.autowalk else 1.0)
            fx, fz = math.sin(self.yaw), math.cos(self.yaw)
            rx, rz = math.cos(self.yaw), -math.sin(self.yaw)
            nx = self.x + (fx * f + rx * s) * speed * dt
            nz = self.z + (fz * f + rz * s) * speed * dt
            hits = self.world.collisions(nx, nz, self.BODY_RADIUS, 1.80)
            if hits:
                for h in hits:
                    if t - self._contact_last.get(h, -9) > 1.5:
                        self.contacts.append({"sim_t": round(t, 2), "object": h, "x": round(nx, 2), "z": round(nz, 2)})
                        self._contact_last[h] = t
                if not self.autowalk:
                    nx, nz = self.x, self.z  # blocked: a real body stops
            self.x, self.z = nx, nz
            self.step_phase += dt * 2 * math.pi * 1.8
        self.world.update(t)

    def _autowalk_controls(self):
        if self.route_i >= len(self.route):
            self.reset_pose()
            return 0.0, 0.0, 0.0
        tx, tz = self.route[self.route_i]
        dx, dz = tx - self.x, tz - self.z
        if math.hypot(dx, dz) < 0.15:
            self.route_i += 1
            return 0.0, 0.0, 0.0
        want = math.atan2(dx, dz)
        err = (want - self.yaw + math.pi) % (2 * math.pi) - math.pi
        turn = max(-1.0, min(1.0, err * 3.0))
        f = 1.0 if abs(err) < math.radians(35) else 0.25
        return f, 0.0, turn

    # -------------------------------------------------------------- capture
    def sensor_pose(self):
        moving = self.autowalk or any(abs(v) > 1e-3 for v in self.move.values())
        bob_y = bob_p = 0.0
        if self.head_bob and moving:
            bob_y = 0.015 * math.sin(2 * self.step_phase)
            bob_p = 1.5 * math.sin(self.step_phase)
        pitch = math.radians(self.mount_pitch + bob_p)
        R = camera_rotation(self.yaw, pitch)
        o = np.array([self.x + self.SENSOR_FORWARD * math.sin(self.yaw), self.mount_height + bob_y,
                      self.z + self.SENSOR_FORWARD * math.cos(self.yaw)], np.float32)
        return o, R, pitch

    def _extra(self, o, pitch) -> dict:
        return {"pose": {"x": round(float(o[0]), 4), "z": round(float(o[2]), 4), "yaw": round(math.degrees(self.yaw), 3),
                         "height": round(float(o[1]), 4), "pitch": round(math.degrees(pitch), 3),
                         "body_x": round(self.x, 4), "body_z": round(self.z, 4)},
                "movers": self.world.mover_states(), "contacts": len(self.contacts),
                "scene_version": self.scene_version}

    def capture(self, t: float):
        """Render one frame in-process (tests, bench and protocol tools)."""
        self.world.update(t)
        o, R, pitch = self.sensor_pose()
        depth = self.world.render(o, R, self.cam, self.rng, sun=self.sun, blocked=self.blocked)
        return depth, self._extra(o, pitch)

    def spec(self, cfg: dict) -> dict:
        return {k: self.sensor[k] for k in SPEC_KEYS} | {"preset": self.preset}

    has_rgb = True

    def set_rgb(self, on: bool):
        self.want_rgb = bool(on)
        if not on:
            self._rgb = None

    def rgb(self):
        if self._rgb is None:
            return None
        intr = dict(self.intrinsics, baseline=0.0, label="Simulated camera (material colours)")
        return self._rgb[0], intr, self._rgb[1]

    def info(self):
        d = super().info()
        d.update(scene=self.world.scene.get("id"), sun=self.sun, blocked=self.blocked, autowalk=self.autowalk,
                 head_bob=self.head_bob, mount_height=self.mount_height, mount_pitch=self.mount_pitch)
        return d


def _render_worker(conn, sensor_cfg: dict, seed: int):
    """Child process: owns a copy of the world and renders depth frames on request."""
    cam = CameraModel.from_config(sensor_cfg)
    rng = np.random.default_rng(seed)
    world = None
    while True:
        try:
            msg = conn.recv()
        except (EOFError, OSError):
            return
        if msg is None:
            return
        if msg[0] == "scene":
            world = World(msg[1])
        elif msg[0] == "render":
            _, t, o, R, sun, blocked, want_rgb = msg
            world.update(t)  # same t as the parent's world: pedestrians stay in sync
            out = world.render(o, R, cam, rng, sun=sun, blocked=blocked, want_rgb=want_rgb)
            if want_rgb:
                conn.send_bytes(out[0].tobytes())
                conn.send_bytes(out[1].tobytes())
            else:
                conn.send_bytes(out.tobytes())
