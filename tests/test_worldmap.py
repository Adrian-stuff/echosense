"""World map (processing/worldmap.py): scan matching and the rolling map."""
from __future__ import annotations

import math

import numpy as np
import pytest

from echosense.config import load_config
from echosense.processing.pipeline import DepthProcessor
from echosense.processing.worldmap import WorldMapper, distance_field, level_rotation
from echosense.sensors.base import DepthFrame
from echosense.sensors.simulated import SimulatedKinect


@pytest.fixture
def cfg():
    return load_config()


def corner_points():
    """A wall across the path, a wall to the right and three poles; walls centred in 5 cm cells."""
    return np.concatenate([
        np.stack([np.linspace(-2, 2, 80), np.full(80, 3.025)], 1),
        np.stack([np.full(50, 1.525), np.linspace(0.5, 3, 50)], 1),
        np.array([[-1.0, 1.5], [-0.5, 2.2], [0.6, 1.2]]),
    ])


def seen_from(P, dx, dz, dyaw_deg):
    """Body-frame view of world points from pose (dx, dz, yaw)."""
    c, s = math.cos(math.radians(dyaw_deg)), math.sin(math.radians(dyaw_deg))
    X, Z = P[:, 0] - dx, P[:, 1] - dz
    return c * X - s * Z, s * X + c * Z


def test_level_rotation_takes_floor_normal_to_up():
    n = np.array([0.05, 0.98, -0.17])
    R = level_rotation(n)
    assert np.allclose(R @ (n / np.linalg.norm(n)), [0, 1, 0], atol=1e-9)
    assert np.allclose(R @ R.T, np.eye(3), atol=1e-9)


def test_distance_field_measures_distance_to_nearest_surface():
    occ = np.zeros((20, 20), bool)
    occ[10, 10] = True
    d = distance_field(occ, 0.05, 4)
    assert d[10, 10] == pytest.approx(0.0, abs=1e-6)
    assert d[10, 12] == pytest.approx(0.10, abs=1e-3)
    assert d[12, 13] == pytest.approx(0.05 * math.sqrt(13), abs=1e-3)
    assert d[0, 0] == pytest.approx(0.20, abs=1e-6)  # capped at radius cells


@pytest.mark.parametrize("move, tol_m, tol_deg", [((0, 0, 0), 0.01, 0.2), ((0, 0.05, 0), 0.01, 0.3), ((0, 0, 3), 0.01, 0.3)])
def test_matcher_recovers_a_known_move(move, tol_m, tol_deg):
    P = corner_points()
    m = WorldMapper()
    for _ in range(3):
        m._insert(P[:, 0], P[:, 1], np.ones(len(P)), *(np.zeros(0),) * 4, 0.0)
    bx, bz = seen_from(P, *move)
    est, matched, _ = m._match(bx, bz, np.zeros(3))
    assert matched
    assert math.hypot(est[0] - move[0], est[1] - move[1]) < tol_m
    assert abs(math.degrees(est[2]) - move[2]) < tol_deg


def test_matcher_rejects_an_impossible_jump():
    """A 40 cm / 15 deg jump in one frame isn't walking: keep the motion model's guess."""
    P = corner_points()
    m = WorldMapper()
    for _ in range(3):
        m._insert(P[:, 0], P[:, 1], np.ones(len(P)), *(np.zeros(0),) * 4, 0.0)
    est, matched, _ = m._match(*seen_from(P, 0.3, 0.3, 15), np.zeros(3))
    assert not matched and np.allclose(est, 0)


def walk(cfg, scene, move, seconds, still=6):
    sim = SimulatedKinect(cfg, scene, seed=1)
    sim.head_bob = False
    proc = DepthProcessor(cfg)
    res = None
    for f in range(int(seconds * 30)):
        t = f / 30
        sim.move = move if f >= still else {"f": 0, "s": 0, "turn": 0}
        sim._step(1 / 30, t)
        depth, extra = sim.capture(t)
        res = proc.process(DepthFrame(depth, t, f + 1, sim.intrinsics, "sim", extra), 4.5)
    return proc, res


def test_world_map_follows_a_straight_walk(cfg):
    proc, res = walk(cfg, "maysan_street", {"f": 1, "s": 0, "turn": 0}, 3.0)
    w = res.world
    assert w["z"] > 2.5  # walked about 2.8 m
    assert w["err_pos"] < 0.15 and w["err_yaw"] < 2.0


def test_world_map_follows_a_turn_on_the_spot(cfg):
    proc, res = walk(cfg, "obstacle_course", {"f": 0, "s": 0, "turn": 0.5}, 3.0)
    w = res.world
    assert abs(w["yaw"]) > 80  # turned about 100 deg
    assert w["err_yaw"] < 4.0 and w["err_pos"] < 0.2


def test_world_map_remembers_what_went_out_of_view(cfg):
    """The box ahead at the start is still in the map after turning ~95 deg away from it."""
    proc, _ = walk(cfg, "obstacle_course", {"f": 0, "s": 0, "turn": 0}, 0.3)
    m = proc.mapper

    def box_cells():
        n, c = m.n, m.cell
        xs = m.x0 + (np.arange(n) + 0.5) * c
        zs = m.z0 + (np.arange(n) + 0.5) * c
        sel = (np.abs(xs)[None, :] < 0.6) & (zs[:, None] > 2.0) & (zs[:, None] < 3.5)
        return int(((m.E >= m.c["occupied"]) & sel).sum())

    before = box_cells()
    assert before > 5
    sim = SimulatedKinect(cfg, "obstacle_course", seed=2)
    sim.head_bob = False
    sim.move = {"f": 0, "s": 0, "turn": 1.0}
    for f in range(40):  # ~1.3 s at 75 deg/s
        t = 0.3 + f / 30
        sim._step(1 / 30, t)
        depth, extra = sim.capture(t)
        proc.process(DepthFrame(depth, t, 100 + f, sim.intrinsics, "sim", extra), 4.5)
    assert abs(m.state()["yaw"]) > 60
    assert box_cells() >= 0.5 * before


def test_world_map_off_and_reset(cfg):
    off = load_config()
    off["processing"]["world_map"] = {"enabled": False}
    assert DepthProcessor(off).mapper is None
    proc, res = walk(cfg, "maysan_street", {"f": 1, "s": 0, "turn": 0}, 1.0)
    assert res.world["travelled"] > 0.3 and proc.mapper.E.max() > 0
    snap = proc.mapper.snapshot()
    assert snap["w"] == snap["h"] == 100 and len(snap["trail"]) > 2
    proc.reset()
    assert proc.mapper.E.max() == 0 and proc.mapper.state()["travelled"] == 0
