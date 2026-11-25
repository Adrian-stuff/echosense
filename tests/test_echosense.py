"""Echo-Sense test-suite. Runs on the laptop, in the Docker virtual Orange Pi, and on the board."""
from __future__ import annotations

import json
import math
import os
import time

import numpy as np
import pytest

from echosense.audio.engine import AudioEngine
from echosense.audio.hrtf import StructuralHRTF, itd_ild
from echosense.audio.sonify import Sonifier
from echosense.config import load_config
from echosense.device.monitors import HealthMonitor
from echosense.device.settings import SettingsStore
from echosense.device.training import Trainer
from echosense.processing.pipeline import DepthProcessor
from echosense.sensors.base import DepthFrame
from echosense.sensors.kinect2 import find_bridge
from echosense.sensors.replay import Recorder, ReplaySource, read_recording
from echosense.sensors.simulated import SimulatedKinect
from echosense.tools.protocol import lab_scene


@pytest.fixture
def cfg():
    return load_config()


def scene(*objects, ground="concrete"):
    return {"id": "t", "name": "test", "ground": ground, "start": {"x": 0, "z": 0, "yaw": 0}, "objects": list(objects)}


def run(cfg, sc, frames=5, seed=1, **sim_kw):
    sim = SimulatedKinect(cfg, sc, seed=seed)
    sim.head_bob = False
    for k, v in sim_kw.items():
        setattr(sim, k, v)
    proc = DepthProcessor(cfg)
    proc.set_mount(cfg["sensor"]["mount_height"], cfg["sensor"]["mount_pitch_deg"])
    res = None
    for i in range(frames):
        depth, extra = sim.capture(i / 30)
        res = proc.process(DepthFrame(depth, i / 30, i + 1, sim.intrinsics, "sim", extra), 4.5, True)
    return sim, proc, res


# ----------------------------------------------------------------- sensor model
def test_sim_floor_depth_matches_geometry(cfg):
    sim = SimulatedKinect(cfg, scene(), seed=0)
    sim.head_bob = False
    depth, _ = sim.capture(0)
    o, R, _ = sim.sensor_pose()
    v = 400  # a row that sees the floor
    d = sim.cam.dirs[v, 256] @ R.T
    expected = -o[1] / d[1]
    assert abs(float(np.median(depth[v, 250:262])) - expected) < 0.01


def test_sim_invalid_outside_range(cfg):
    sim = SimulatedKinect(cfg, scene(), seed=0)
    depth, _ = sim.capture(0)
    valid = depth[depth > 0]
    assert valid.min() >= cfg["sensor"]["min_depth"] and valid.max() <= cfg["sensor"]["max_depth"]
    assert (depth[:20] == 0).all()  # top rows look over the floor into empty space


def test_glass_is_partly_transparent(cfg):
    conc = run(cfg, lab_scene(2.0, 0, "concrete"), frames=1)[0].capture(0)[0]
    glass = run(cfg, lab_scene(2.0, 0, "glass"), frames=1)[0].capture(0)[0]
    centre = (slice(150, 170), slice(250, 262))
    hit = lambda d: float(((d[centre] > 1.9) & (d[centre] < 2.1)).mean())  # noqa: E731
    assert hit(conc) > 0.9 and hit(glass) < 0.4


# ------------------------------------------------------------------- processing
@pytest.mark.parametrize("d", [1.0, 2.0, 3.0, 4.0])
def test_distance_error_within_15cm(cfg, d):
    _, _, res = run(cfg, lab_scene(d, 0, "concrete"))
    hits = [o for o in res.reported if abs(o["az"]) < 10]
    assert hits, f"panel at {d} m not detected"
    assert abs(hits[0]["r"] - d) < 0.15


@pytest.mark.parametrize("az", [-30, -15, 15, 30])
def test_bearing(cfg, az):
    _, _, res = run(cfg, lab_scene(2.0, az, "concrete"))
    assert any(abs(o["az"] - az) < 5 for o in res.reported)


def test_no_false_positives_on_open_ground(cfg):
    sim = SimulatedKinect(cfg, "open_area", seed=4)
    proc = DepthProcessor(cfg)
    fp = 0
    for i in range(60):
        sim.step_phase = i * 0.4  # walking bob without moving
        depth, _ = sim.capture(i / 30)
        res = proc.process(DepthFrame(depth, i / 30, i + 1, sim.intrinsics, "sim"), 4.0, True)
        fp += bool(res.reported)
    assert fp == 0


def test_confirmation_suppresses_single_frame_detections(cfg):
    sim = SimulatedKinect(cfg, lab_scene(2.0, 0, "concrete"), seed=1)
    proc = DepthProcessor(cfg)
    n = proc.confirm_frames
    for i in range(n):
        res = proc.process(DepthFrame(sim.capture(i / 30)[0], i / 30, i + 1, sim.intrinsics, "sim"), 4.0)
        assert bool(res.reported) == (i == n - 1)


def test_head_level_overhang(cfg):
    board = {"type": "box", "name": "sign", "x": 0, "z": 2.12, "w": 0.8, "h": 0.3, "d": 0.04, "y0": 1.5, "material": "wood"}
    _, _, res = run(cfg, scene(board))
    assert [o["kind"] for o in res.reported] == ["head"]
    assert abs(res.reported[0]["r"] - 2.0) < 0.1


def test_low_obstacle(cfg):
    box = {"type": "box", "name": "curb", "x": 0, "z": 2.32, "w": 0.6, "h": 0.3, "d": 0.4, "material": "concrete"}
    _, _, res = run(cfg, scene(box))
    assert res.reported and res.reported[0]["kind"] == "low"


def test_drop_off_lip_distance(cfg):
    pit = {"type": "pit", "name": "drain", "x0": -0.6, "x1": 0.6, "z0": 2.12, "z1": 2.8, "depth": 0.5}
    _, _, res = run(cfg, scene(pit))
    drops = [o for o in res.reported if o["kind"] == "drop"]
    assert drops and abs(drops[0]["r"] - 2.0) < 0.12


def test_ground_plane_estimates_mount_pitch(cfg):
    sim = SimulatedKinect(cfg, scene(), seed=2)
    sim.head_bob = False
    sim.mount_pitch = -14.0  # sensor actually tilted 4 deg more than configured
    sim.mount_height = 1.30
    proc = DepthProcessor(cfg)  # still believes -10 deg, 1.25 m
    for i in range(10):
        proc.process(DepthFrame(sim.capture(i / 30)[0], i / 30, i + 1, sim.intrinsics, "sim"), 4.0)
    g = proc.ground_info()
    assert g["ok"] and abs(g["pitch"] - -14.0) < 0.8 and abs(g["height"] - 1.30) < 0.04


def test_corridor_filter(cfg):
    pole = {"type": "cylinder", "name": "pole", "x": 1.2, "z": 2.0, "r": 0.1, "y1": 2.5}
    sim, proc, _ = run(cfg, scene(pole))
    res = proc.process(DepthFrame(sim.capture(1)[0], 1.0, 99, sim.intrinsics, "sim"), 4.0, report_off_path=False)
    assert res.reported == []
    assert any(not t.in_path for t in res.tracks)


# ------------------------------------------------------------------------ audio
def test_hrtf_lateralisation():
    h = StructuralHRTF(48000)
    for az in (15, 45, 60):
        itd_r, ild_r = itd_ild(h.hrir(az), 48000)
        itd_l, ild_l = itd_ild(h.hrir(-az), 48000)
        assert itd_r > 0 and ild_r > 0 and itd_l < 0 and ild_l < 0
    assert abs(itd_ild(h.hrir(0), 48000)[0]) < 1e-5
    assert 550e-6 < itd_ild(h.hrir(90), 48000)[0] < 800e-6


def test_limiter_enforces_ceiling():
    e = AudioEngine(48000, 1024, max_output_dbfs=-6.0)
    e.schedule(np.ones((4096, 2), np.float32) * 3.0)
    peaks = [float(np.abs(e.render(1024)).max()) for _ in range(4)]
    assert max(peaks) <= 10 ** (-6 / 20) + 1e-6


def test_sonifier_rate_and_immediate_first_ping():
    son = Sonifier(48000, StructuralHRTF(48000), range_limit=4.0)
    assert son.interval(0.8, 0) < son.interval(2.0, 0) < son.interval(3.8, 0)
    assert son.gain(0.8, 0) > son.gain(3.0, 0)
    e = AudioEngine(48000, 1024)
    e.generators.append(son)
    onsets = []
    son.on_ping = lambda cue, t, first: onsets.append(first)
    son.set_cues([{"id": 1, "kind": "body", "r": 1.0, "az": 20.0, "el": 0.0, "rank": 0}])
    out = e.render(1024)
    assert onsets and onsets[0] is True
    assert np.abs(out[:, 1]).sum() > np.abs(out[:, 0]).sum()  # source on the right -> right ear louder


# ----------------------------------------------------------------------- device
def test_settings_persist(tmp_path):
    p = tmp_path / "s.json"
    s = SettingsStore(p)
    s.update(volume=9, range="far", mode="warning")
    s2 = SettingsStore(p)
    assert (s2.s.volume, s2.s.range, s2.s.mode) == (9, "far", "warning")


def test_health_blocked_and_no_data(cfg):
    h = HealthMonitor(cfg["health"])
    h.on_frame(0.0, 0.6)
    assert h.tick(0.0) == "ok"
    for t in np.arange(0.1, 1.5, 0.1):
        h.on_frame(t, 0.01)
        h.tick(t)
    assert h.state == "blocked"
    assert h.tick(4.0) == "no_data"


def test_training_angular_error():
    tr = Trainer()
    tr.start()
    tr.set_target(az=30, r=1.5)
    assert tr.respond(18)["error_deg"] == 12
    assert tr.summary()["mae_deg"] == 12


def test_recording_roundtrip(tmp_path, cfg):
    sim = SimulatedKinect(cfg, "obstacle_course", seed=1)
    path = str(tmp_path / "rec.esd")
    rec = Recorder(path, sim.intrinsics, "sim")
    frames = []
    for i in range(3):
        d, _ = sim.capture(i / 30)
        frames.append(d)
        rec.write(DepthFrame(d, i / 30, i + 1, sim.intrinsics, "sim"))
    rec.close()
    back = [d for _, _, d in read_recording(path)]
    assert len(back) == 3 and np.abs(back[1] - frames[1]).max() < 0.0011
    src = ReplaySource(path)
    src.start()
    f = src.read(2.0)
    src.stop()
    assert f is not None and f.depth.shape == (424, 512)


@pytest.mark.skipif(not find_bridge(), reason="echosense-kinect2 bridge not built")
def test_kinect2_bridge_fake_stream(cfg):
    from echosense.sensors import open_source

    src = open_source("kinect2-fake", cfg)
    try:
        f = src.read(3.0)
        assert f is not None and f.depth.shape == (424, 512) and (f.depth > 0).mean() > 0.2
    finally:
        src.stop()


def test_app_end_to_end(tmp_path, cfg):
    from echosense.app import EchoSense

    cfg["speech"]["engine"] = "none"
    cfg["paths"]["logs"] = str(tmp_path / "logs")
    app = EchoSense(cfg, source="sim", scene="obstacle_course", audio="none", settings_path=str(tmp_path / "s.json"))
    app.start()
    try:
        app.sim.set_pose(0.0, 1.0, 0.0)  # box obstacle ~1.7 m ahead
        time.sleep(2.5)
        m = app.metrics()
        assert m["fps"] > 5  # the simulated sensor is slower on constrained arm64 (virtual Orange Pi)
        assert app.health.state == "ok"
        assert app.cues and app.cues[0]["kind"] == "body"
        assert app.sonifier.pings > 0
        app.action("volume_up")
        assert app.settings.s.volume == 7
        app.sim_command("blocked", True)
        time.sleep(2.5)
        assert app.health.state == "blocked" and app.cues == []
    finally:
        app.stop()


# ------------------------------------------------------------------ performance
def _sample(**kw):
    base = {"source": "kinect2", "fps": 30.0, "sensor_fps": 30, "dropped_pct": 0.0, "proc_p95": 8.0, "e2e_p95": 150.0,
            "underruns_60s": 0, "audio_load": 2.0, "cpu_device": 20.0, "cpu_driver": 15.0, "cpu_max_core": 50.0,
            "cpu_total": 30.0, "cpu_container": None, "temp_c": 55.0, "freq_mhz": 2200, "throttled": False,
            "mem_avail_pct": 60.0, "rss_mb": 80.0}
    base.update(kw)
    return base


def _status(checks, name):
    return next(c["status"] for c in checks if c["name"] == name)


def test_perf_checks(tmp_path, cfg):
    from echosense.app import EchoSense

    cfg["speech"]["engine"] = "none"
    app = EchoSense(cfg, source="sim", audio="none", settings_path=str(tmp_path / "s.json"))
    pm = app.perf
    ok = pm.checks(_sample())
    assert all(c["status"] in ("ok", "n/a") for c in ok)
    bad = pm.checks(_sample(fps=8.0, dropped_pct=40.0, proc_p95=60.0, e2e_p95=420.0, temp_c=88.0, underruns_60s=3))
    for name in ("Frame rate", "Keeping up with the sensor", "Processing time (p95)",
                 "Obstacle-to-sound latency (p95)", "SoC temperature", "Audio dropouts (last 60 s)"):
        assert _status(bad, name) == "fail", name
    # a slow simulator that the device fully keeps up with is not the device's fault
    assert _status(pm.checks(_sample(source="sim", fps=9.0, dropped_pct=0.0)), "Frame rate") == "n/a"
    assert _status(pm.checks(_sample(cpu_device=70.0, cpu_driver=25.0)), "Device software CPU") == "fail"


def test_perf_monitor_samples_live_app(tmp_path, cfg):
    from echosense.app import EchoSense

    cfg["speech"]["engine"] = "none"
    csv_path = tmp_path / "perf.csv"
    app = EchoSense(cfg, source="sim", scene="obstacle_course", audio="none", settings_path=str(tmp_path / "s.json"),
                    perf_csv=str(csv_path))
    app.start()
    try:
        time.sleep(3.3)
        s = app.perf.latest
        assert s and s["fps"] > 3 and s["verdict"] and len(s["checks"]) >= 10
        assert s["cpu_simulator"] > 0  # simulator CPU is attributed to the simulator, not the device
    finally:
        app.stop()
    assert len(csv_path.read_text().splitlines()) >= 3


# ------------------------------------------------------------- depth sensing
def test_mixed_pixels_removed():
    from echosense.processing.depthfilter import remove_mixed_pixels

    d = np.full((4, 9), 3.0, np.float32)
    d[:, :4] = 1.0
    d[:, 4] = 2.0  # a pixel between the 1 m object and the 3 m background
    out = remove_mixed_pixels(d)
    assert (out[:, 4] == 0).all() and (out[:, :4] == 1.0).all() and (out[:, 5:] == 3.0).all()


@pytest.mark.parametrize("profile", [None, "orangepi5"])
def test_thin_pole_far_away(profile):
    cfg = load_config(profile=profile)
    pole = {"type": "cylinder", "name": "pole", "x": 0.05, "z": 0.12 + 4.0 + 0.025, "r": 0.025, "y1": 2.2, "material": "metal"}
    _, _, res = run(cfg, scene(pole), frames=6)
    hits = [o for o in res.reported if abs(o["r"] - 4.0) < 0.3]
    assert hits and abs(hits[0]["r"] - 4.0) < 0.05


def test_noisy_surface_in_sun_within_15cm():
    """Foliage in direct sun at 4 m: the noisiest case. On the device profile the error stays within
    the proposal's 15 cm, and any bias is towards the user (reported nearer), the safe side."""
    cfg = load_config(profile="orangepi5")
    errs = []
    for seed in range(3):
        _, _, res = run(cfg, lab_scene(4.0, 0, "foliage"), frames=8, seed=seed, sun=0.9)
        hits = [o for o in res.reported if abs(o["az"]) < 10]
        assert hits
        errs.append(hits[0]["r"] - 4.0)
    assert np.mean(np.abs(errs)) < 0.15 and max(errs) < 0.03


def test_time_to_contact_for_approaching_person(cfg):
    sc = scene({"type": "pedestrian", "name": "w", "path": [[0.0, 4.82], [0.0, 0.92]], "speed": 1.3})
    sim = SimulatedKinect(cfg, sc, seed=3)
    sim.head_bob = False
    proc = DepthProcessor(cfg)
    for f in range(45):
        res = proc.process(DepthFrame(sim.capture(f / 30)[0], f / 30, f + 1, sim.intrinsics, "sim"), 4.5, True)
    o = res.reported[0]
    assert abs(o["vel_r"] + 1.3) < 0.15
    assert o["ttc"] is not None and abs(o["ttc"] - o["r"] / 1.3) < 0.2
    assert o["r_urgent"] < o["r"]  # closing faster than walking pace: sounds more urgent than its distance


def test_parallel_curb_not_cued_but_hole_ahead_is(cfg):
    curb = {"type": "pit", "name": "street", "x0": 0.7, "x1": 5.0, "z0": -1.0, "z1": 20.0, "depth": 0.15}
    hole = {"type": "pit", "name": "drain", "x0": -0.6, "x1": 0.4, "z0": 2.12, "z1": 2.7, "depth": 0.5}
    _, _, res = run(cfg, scene(curb), frames=6)
    assert not [o for o in res.reported if o["kind"] == "drop"]
    _, _, res = run(cfg, scene(curb, hole), frames=6)
    drops = [o for o in res.reported if o["kind"] == "drop"]
    assert drops and abs(drops[0]["r"] - 2.0) < 0.15


def test_degraded_sensing_state(cfg):
    h = HealthMonitor(cfg["health"])
    for t in np.arange(0, 3, 0.1):
        h.on_frame(t, 0.6, 0.4)
        h.tick(t)
    assert h.state == "degraded"
    for t in np.arange(3, 6, 0.1):
        h.on_frame(t, 0.6, 0.97)
        h.tick(t)
    assert h.state == "ok"


def test_sunlight_lowers_sensing_quality(cfg):
    q = {}
    for sun in (0.0, 1.0):
        _, _, res = run(cfg, "open_area", frames=3, sun=sun)
        q[sun] = res.quality
    assert q[0.0] > 0.95 and q[1.0] < cfg["health"]["degraded_quality"]


def test_sensor_presets_and_coverage(cfg):
    from echosense.sensors.presets import PRESETS, coverage

    k = coverage(PRESETS["kinect_v2"], 1.25, -10)
    w = coverage(PRESETS["azure_wfov"], 1.25, -10)
    assert 69 < k["hfov"] < 72 and w["hfov"] == 120.0
    assert w["floor_from"] < k["floor_from"] and w["head_from"] < k["head_from"]
    cfg["sim"]["sensor_preset"] = "azure_wfov"
    cfg["processing"]["stride"] = 4
    sim, proc, res = run(cfg, lab_scene(2.0, 0, "concrete"))
    assert sim.cam.width == 512 and sim.cam.height == 512
    assert proc.stride_used == 2  # coarser pixels -> finer stride
    assert any(abs(o["r"] - 2.0) < 0.1 for o in res.reported)


# ------------------------------------------------------- object recognition / lite
def test_object_model_is_small_and_fast():
    from echosense.processing.objects import FEATURES, MODEL_PATH, ObjectRecognizer

    rec = ObjectRecognizer()
    assert rec.available and MODEL_PATH.stat().st_size < 256 * 1024
    x = np.zeros(len(FEATURES), np.float32)
    t = time.perf_counter()
    for _ in range(1000):
        rec.proba(x)
    assert (time.perf_counter() - t) / 1000 < 1e-3


@pytest.mark.parametrize("cls,obj,expect", [
    ("person", {"type": "pedestrian", "name": "p", "path": [[0.0, 2.6], [0.0, 2.601]], "speed": 0.0}, "person"),
    ("pole", {"type": "cylinder", "name": "pole", "x": 0.0, "z": 2.6, "r": 0.12, "y1": 5.0}, "pole"),
    ("overhang", {"type": "box", "name": "s", "x": 0, "z": 2.6, "w": 0.8, "h": 0.3, "d": 0.05, "y0": 1.55}, "overhang"),
    ("low", {"type": "box", "name": "b", "x": 0, "z": 2.6, "w": 0.8, "h": 0.3, "d": 0.4}, "low"),
])
def test_recognises_common_obstacles(cfg, cls, obj, expect):
    _, _, res = run(cfg, scene(obj), frames=10)
    assert res.reported and res.reported[0]["label"] == expect


def test_whats_ahead_phrase(tmp_path, cfg):
    from echosense.app import EchoSense

    cfg["speech"]["engine"] = "none"
    app = EchoSense(cfg, source="sim", scene=scene({"type": "cylinder", "name": "pole", "x": 0.0, "z": 1.62, "r": 0.12, "y1": 5.0}),
                    audio="none", settings_path=str(tmp_path / "s.json"))
    app.start()
    try:
        time.sleep(1.5)
        assert app.describe_scene() == "Pole, 1.5 meters, ahead."
    finally:
        app.stop()


def test_orange_pi_one_profile_runs_lite(tmp_path):
    cfg = load_config(profile="orangepi_one")
    assert cfg["sensor"]["fps"] == 15 and cfg["sim"]["sensor_preset"] == "kinect_v1"
    sim, proc, res = run(cfg, lab_scene(2.0, 0, "concrete"), frames=5)
    assert sim.cam.width == 640 and sim.fps == 15
    assert proc.stride_used == 8 and proc.front.prestride == 2
    assert any(abs(o["r"] - 2.0) < 0.1 for o in res.reported)


@pytest.mark.parametrize("near", [True, False])
def test_near_mode_keeps_warning_inside_minimum_range(cfg, near):
    pole = {"type": "cylinder", "name": "pole", "x": 0.0, "z": 2.0, "r": 0.15, "y1": 2.5, "material": "concrete"}
    sim = SimulatedKinect(cfg, scene(pole), seed=1)
    sim.head_bob = False
    proc = DepthProcessor(cfg)
    proc.near_mode = near
    t = 0.0
    seen_close = False
    for f in range(60):  # walk at 1 m/s from 1.85 m to 0.25 m from the pole surface (sensor minimum is 0.5 m)
        t = f / 30
        sim.z = min(1.6, f / 30)
        res = proc.process(DepthFrame(sim.capture(t)[0], t, f + 1, sim.intrinsics, "sim"), 2.5)
        if res.reported and res.reported[0]["r"] < 0.9:
            seen_close = True
    assert seen_close
    t += 0.5  # half a second after the pole went inside the minimum range
    res = proc.process(DepthFrame(sim.capture(t)[0], t, 99, sim.intrinsics, "sim"), 2.5)
    still = [o for o in res.reported if o["memory"]]
    assert bool(still) == near
    if near:
        assert still[0]["r"] < 0.5


def test_camera_stream_only_when_watched(tmp_path, cfg):
    from echosense.app import EchoSense

    cfg["speech"]["engine"] = "none"
    app = EchoSense(cfg, source="sim", scene="maysan_street", audio="none", settings_path=str(tmp_path / "s.json"))
    packets = []
    app.rgb_listeners.append(packets.append)
    app.start()
    try:
        time.sleep(1.0)
        assert app.source.rgb() is None and not packets  # nobody watching: no colour rendering at all
        app.set_rgb_viewers(1)
        time.sleep(1.5)
        img, intr, _ = app.source.rgb()
        assert img.shape == (424, 512, 3) and img.dtype == np.uint8 and img.std() > 10
        assert packets and packets[-1][0] == 3
        app.set_rgb_viewers(0)
    finally:
        app.stop()


@pytest.mark.skipif(not find_bridge(), reason="echosense-kinect2 bridge not built")
def test_reconnects_after_sensor_drops(tmp_path, cfg):
    """Kill the sensor driver process mid-stream (like a USB reset): the app must notice, warn,
    reopen the sensor by itself and resume, without ever switching to fake data."""
    from echosense.app import EchoSense

    cfg["speech"]["engine"] = "none"
    cfg["reconnect"] = {"stall_s": 0.8, "retry_s": 0.5}
    app = EchoSense(cfg, source="kinect2-fake", audio="none", settings_path=str(tmp_path / "s.json"))
    kinds = []
    app.event_listeners.append(lambda e: kinds.append((e["kind"], e["text"])))
    app.start()
    try:
        time.sleep(1.5)
        first = app.source
        assert first.kind == "kinect2" and app.health.state == "ok"
        first.proc.kill()  # the "USB" goes away
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not any("reconnected" in t for _, t in kinds):
            time.sleep(0.2)
        assert any("reconnecting" in t for _, t in kinds)
        assert any("reconnected" in t for _, t in kinds)
        assert app.source is not first and app.source.kind == "kinect2" and app.source.running
        time.sleep(1.0)
        assert app.health.state == "ok"
    finally:
        app.stop()


def test_device_profile_never_falls_back_to_simulator(tmp_path):
    from echosense.app import EchoSense

    cfg = load_config(profile="orangepi5")
    cfg["speech"]["engine"] = "none"
    cfg["sensor"]["kinect2_bridge"] = "/nonexistent"
    app = EchoSense(cfg, source="kinect1-missing", audio="none", settings_path=str(tmp_path / "s.json"))
    app.start()
    try:
        time.sleep(1.5)
        assert app.source.kind == "none"  # no sensor: keep searching and warn, never fake depth
        assert app.health.state in ("starting", "no_data")
    finally:
        app.stop()


@pytest.mark.parametrize("height,pitch", [(0.75, -12.0), (1.0, -20.0), (1.4, -5.0), (0.8, 0.0)])
def test_floor_found_when_mount_differs_from_settings(height, pitch):
    """A Kinect on a desk, in the hand or strapped differently: the floor must still be the floor,
    not one huge obstacle (or drop-off) that sounds all the time."""
    cfg = load_config()
    cfg["sim"]["sensor_preset"] = "kinect_v1"
    sim = SimulatedKinect(cfg, "open_area", seed=3)
    sim.mount_height, sim.mount_pitch, sim.head_bob = height, pitch, False
    proc = DepthProcessor(cfg)  # still configured for 1.25 m, -10 deg
    cued = 0
    for i in range(30):
        res = proc.process(DepthFrame(sim.capture(i / 30)[0], i / 30, i + 1, sim.intrinsics, "sim"), 2.5)
        cued += bool(res.reported)
    assert cued == 0
    assert abs(res.ground["height"] - height) < 0.05 and abs(res.ground["pitch"] - pitch) < 1.5
    g = proc.calibrate_floor()
    assert abs(g["height"] - height) < 0.05 and abs(g["pitch"] - pitch) < 1.5


def test_sensitivity_levels(cfg):
    proc = DepthProcessor(cfg)
    assert proc.sensitivity == "normal"
    counts = {}
    for level in ("high", "normal", "low"):
        proc.set_sensitivity(level)
        counts[level] = proc.confirm_frames
    assert counts["high"] < counts["normal"] < counts["low"]
    # an obstacle that is only seen in every other frame (flicker) is not confirmed at normal sensitivity
    from echosense.processing.pipeline import Obstacle
    blip = Obstacle("body", 2.0, 0.0, 0.0, 2.0, 0.2, 1.2, -0.2, 0.2, 1.95, 2.05, 200, True)
    for level, expect in (("high", True), ("normal", False)):
        proc = DepthProcessor(cfg)
        proc.set_sensitivity(level)
        for i in range(16):
            proc._track([blip] if i % 2 == 0 else [], i / 30)
        assert any(t.confirmed for t in proc.tracks) == expect, level
    # but a steadily seen one is, at every level
    for level in ("high", "normal", "low"):
        cfg2 = load_config()
        cfg2["default_sensitivity"] = level
        _, _, res = run(cfg2, lab_scene(2.0, 0, "concrete"), frames=12)
        assert any(abs(o["r"] - 2.0) < 0.1 for o in res.reported), level


def test_sensitivity_setting_persists_and_limits_cues(tmp_path, cfg):
    from echosense.app import EchoSense
    app = EchoSense(cfg, source="sim", audio="none", settings_path=str(tmp_path / "s.json"))
    assert app.processor.sensitivity == "normal"
    app.set_setting("sensitivity", "low")
    assert app.processor.sensitivity == "low" and app.processor.sens["max_cues"] == 1
    assert SettingsStore(tmp_path / "s.json").s.sensitivity == "low"
    app.set_setting("sensitivity", "bogus")
    assert app.processor.sensitivity == "low"


def test_announcement_not_repeated_when_track_is_refound(tmp_path, cfg):
    from echosense.app import EchoSense
    app = EchoSense(cfg, source="sim", audio="none", settings_path=str(tmp_path / "s.json"))
    said = []
    app.speech.say = lambda text, *a, **k: said.append(text)
    person = {"label": "person", "label_conf": 0.9, "az": 0.0, "hits": 20}
    app._auto_announce([{**person, "id": 1}], 10.0)
    app._auto_announce([{**person, "id": 2}], 14.0)  # same person, new track id after a dropout
    app._auto_announce([{**person, "id": 3, "hits": 3}], 30.0)  # not seen long enough yet
    assert said == ["Person ahead."]
    app._auto_announce([{**person, "id": 4}], 30.0)
    assert len(said) == 2


def test_parked_vehicle_top_is_not_an_overhang(cfg):
    """The far end of a parked motorcycle's top hides the floor under it: that is not free space."""
    moto = {"type": "box", "name": "m", "x": 0.2, "z": 3.3, "w": 1.9, "h": 1.1, "d": 0.7, "material": "metal"}
    for z in (3.0, 3.3, 3.6, 4.2):
        _, _, res = run(cfg, scene({**moto, "z": z}), frames=8)
        assert not any(o["kind"] == "head" for o in res.reported), z


def test_kind_and_path_changes_settle(cfg):
    from echosense.processing.pipeline import Obstacle
    proc = DepthProcessor(cfg)
    body = Obstacle("body", 2.0, 0.0, 0.0, 2.0, 0.2, 1.2, -0.2, 0.2, 1.95, 2.05, 200, True)
    low = Obstacle("low", 2.0, 0.0, 0.0, 2.0, 0.1, 0.4, -0.2, 0.2, 1.95, 2.05, 200, True)
    for i in range(6):
        proc._track([body], i / 30)
    tr = proc.tracks[0]
    proc._track([low], 6 / 30)
    assert tr.kind == "body"  # one odd frame doesn't change the sound
    proc._track([low], 7 / 30)
    proc._track([low], 8 / 30)
    assert tr.kind == "low"
    off = Obstacle("body", 2.0, 0.0, 0.0, 2.0, 0.2, 1.2, -0.2, 0.2, 1.95, 2.05, 200, False)
    proc._track([off], 9 / 30)
    assert tr.in_path  # leaving the path takes a few frames...
    on = Obstacle("body", 2.0, 0.0, 0.0, 2.0, 0.2, 1.2, -0.2, 0.2, 1.95, 2.05, 200, True)
    for i in range(3):
        proc._track([off], (10 + i) / 30)
    assert not tr.in_path
    proc._track([on], 13 / 30)
    assert tr.in_path  # ...entering it is immediate


def test_forest_model_roundtrip():
    from echosense.processing.objects import FEATURES, Forest, load_model
    rng = np.random.default_rng(0)
    X = rng.normal(size=(400, len(FEATURES))).astype(np.float32)
    y = (X[:, 0] > 0).astype(int) + 2 * (X[:, 3] > 0.5).astype(int)
    f = Forest.fit(X, y, n_trees=5, max_depth=4)
    g = load_model(json.loads(json.dumps(f.to_json())))
    assert (g.predict(X) == f.predict(X)).mean() > 0.99 and (f.predict(X) == y).mean() > 0.9
    assert abs(float(g.proba(X[0]).sum()) - 1.0) < 1e-3


@pytest.mark.parametrize("signs", [(1.0, -1.0, 1.0), (-1.0, 1.0, -1.0), (1.0, -1.0, -1.0)])
def test_accelerometer_axes_learned_from_floor(signs):
    cfg = load_config()
    cfg["sim"]["sensor_preset"] = "kinect_v1"
    sim = SimulatedKinect(cfg, "open_area", seed=2)
    sim.accel_signs, sim.mount_pitch = signs, -14.0
    proc = DepthProcessor(cfg)
    for i in range(45):
        d, ex = sim.capture(i / 30)
        proc.process(DepthFrame(d, i / 30, i + 1, sim.intrinsics, "sim", ex), 2.5)
    assert proc.gravity.status() == "verified"
    assert abs(proc.gravity.angles(44 / 30)["pitch"] - (-14.0)) < 1.5


def test_gravity_keeps_heights_right_without_floor_in_view():
    """Up close to a wall the floor is out of view. With the accelerometer the wall's height band
    stays right; without it the estimate falls back to the configured mount after 2 s."""
    wall = scene({"type": "box", "name": "wall", "x": 0, "z": 5.0, "w": 8, "h": 3, "d": 0.2, "material": "concrete"})
    out = {}
    for acc in (False, True):
        cfg = load_config()
        cfg["sim"]["sensor_preset"] = "kinect_v1"
        sim = SimulatedKinect(cfg, wall, seed=3)
        sim.accel, sim.mount_height, sim.mount_pitch, sim.head_bob = acc, 0.9, -4.0, False
        proc = DepthProcessor(cfg)
        for i in range(60):
            d, ex = sim.capture(i / 30)
            proc.process(DepthFrame(d, i / 30, i + 1, sim.intrinsics, "sim", ex), 2.5)
        sim.z = 3.78  # 1 m from the wall
        for i in range(60, 170):
            d, ex = sim.capture(i / 30)
            res = proc.process(DepthFrame(d, i / 30, i + 1, sim.intrinsics, "sim", ex), 2.5)
        out[acc] = (res.ground, res.reported[0])
    g, wall_seen = out[True]
    assert g["source"] == "gravity" and abs(g["height"] - 0.9) < 0.05
    assert abs(wall_seen["h_min"] - 0.4) < 0.12 and abs(wall_seen["h_max"] - 1.23) < 0.12
    assert out[False][0]["source"] == "none"  # the old behaviour: back to the configured 1.25 m


def test_sensor_pointing_at_floor_is_announced(tmp_path, cfg):
    from echosense.app import EchoSense
    app = EchoSense(cfg, source="sim", audio="none", settings_path=str(tmp_path / "s.json"))
    said = []
    app.speech.say = lambda text, *a, **k: said.append(text)
    g = app.processor.gravity
    g.signs[:] = (0.0, -1.0, 1.0)
    g.verified = True
    pitch = math.radians(-60)
    for i in range(40):  # 1.3 s pointing 60 deg down
        g.update((0.0, 9.8 * math.cos(pitch), 9.8 * math.sin(pitch)), i / 30)
        app._check_pointing(i / 30)
    assert said == [cfg["phrases"]["pointing_floor"]]


def test_kinect1_accel_mirrors_with_image(cfg):
    from echosense.sensors.kinect1 import Kinect1Source

    class FakeAccel:
        latest = (1.0, 9.7, -1.5, time.monotonic())

    src = Kinect1Source.__new__(Kinect1Source)
    src._accel, src.flip = FakeAccel(), False
    assert src.accel() == (1.0, 9.7, -1.5)
    src.flip = True
    assert src.accel() == (-1.0, 9.7, -1.5)
    FakeAccel.latest = (1.0, 9.7, -1.5, time.monotonic() - 2)
    assert src.accel() is None  # stale readings are never used


@pytest.mark.parametrize("pids,opens", [({0x02AD, 0x02AE}, False), ({0x02B0, 0x02AE}, True), (None, True)])
def test_accelerometer_probe_skips_model_1473(cfg, monkeypatch, pids, opens):
    """Model 1473's accelerometer sits behind the audio chip (needs firmware): never probed unless forced."""
    from echosense.sensors import kinect1

    tried = []

    class FakeAccel:
        def open(self):
            tried.append(1)
            raise OSError("no device")

    monkeypatch.setattr(kinect1, "kinect_usb_products", lambda: pids)
    monkeypatch.setattr(kinect1, "FreenectAccel", FakeAccel)
    src = kinect1.Kinect1Source.__new__(kinect1.Kinect1Source)
    src.want_accel, src._accel, src.accel_status = "auto", None, "off"
    src._open_accel()
    assert bool(tried) == opens
    if not opens:
        assert "1473" in src.accel_status


def test_real_kinect1_room_without_floor_has_no_phantom_holes():
    """Recorded on a real Kinect v1 (model 1473) sitting about level on a desk, every second pixel:
    two walls and furniture, no floor in view, mount settings left at 1.58 m / -24 deg from an earlier
    fitting. A sloping piece of furniture used to be taken for the floor, and the room filled with
    phantom holes. Now no floor is invented and the walls say which way is up."""
    from pathlib import Path
    z = np.load(Path(__file__).parent / "data" / "kinect1_room_no_floor.npz")
    intr = json.loads(str(z["intr"]))
    I = {"width": 320, "height": 240, "fx": intr["fx"] / 2, "fy": intr["fy"] / 2, "cx": intr["cx"] / 2, "cy": intr["cy"] / 2}
    cfg = load_config()
    proc = DepthProcessor(cfg)
    proc.set_mount(1.579, -24.18)
    for i in range(30):
        d = z["depth"][i % len(z["depth"])].astype(np.float32) / 1000
        res = proc.process(DepthFrame(d, i / 15, i + 1, I, "kinect1"), 4.0)
    assert not any(o["kind"] == "drop" for o in res.reported)
    assert res.ground["source"] == "walls" and 3.0 < res.ground["pitch"] < 11.0  # about level, tilted up


def test_autowalk_route_end_does_not_deadlock(cfg):
    """The live loop holds the scene lock while stepping; at the end of the route auto-walk resets the
    scene, which takes the lock again. A plain Lock froze the simulator there."""
    import threading

    sim = SimulatedKinect(cfg, "maysan_street", seed=1)
    sim.autowalk = True
    sim.route_i = len(sim.route)
    done = threading.Event()

    def step():
        with sim._scene_lock:
            sim._step(1 / 30, 0.0)
        done.set()

    threading.Thread(target=step, daemon=True).start()
    assert done.wait(5), "simulator deadlocked at the end of the auto-walk route"
    assert sim.route_i == 1
