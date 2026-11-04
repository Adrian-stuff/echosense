"""Echo-Sense test-suite. Runs on the laptop, in the Docker virtual Orange Pi, and on the board."""
from __future__ import annotations

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
    n = cfg["processing"]["confirm_frames"]
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
    from echosense.processing.objects import MODEL_PATH, ObjectRecognizer

    rec = ObjectRecognizer()
    assert rec.available and MODEL_PATH.stat().st_size < 64 * 1024
    x = np.zeros(15, np.float32)
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
