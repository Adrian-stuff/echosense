"""Processing module: depth frame -> ordered list of obstacles.

Implements the proposal's processing stage (System functions C2, C3, C4, C7):

1. project depth pixels to 3D points in the *body frame*
   (X right, Y up from the ground, Z forward, metres) using the chest mount
   height and pitch,
2. fit the ground plane (RANSAC, seeded by the nominal mount pose), so head
   bob and an imperfect mount do not turn the floor into obstacles,
3. label points as ground / obstacle / drop-off (holes, open drainage, curbs),
4. bin obstacle points into a top-down occupancy grid and group connected
   cells into obstacles, each with distance, bearing, height band and kind
   (``body``, ``head`` = overhang with free space below, ``low``, ``drop``),
5. suppress walls running parallel to the walking direction and objects
   outside the walking corridor,
6. track obstacles over time (false-positive suppression: an obstacle has to
   be seen in enough frames, for long enough and in most of the frames since it
   first appeared before it is reported; see ``sensitivity_levels``),
7. order reportable obstacles: in the direct path first, then nearest, with
   head-level hazards and drop-offs promoted.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np
from ..sensors.base import DepthFrame
from .depthfilter import DepthFrontEnd
from .gravity import GravityEstimator
from .objects import CLASSES, ObjectRecognizer, drop_label, shape_features
from .worldmap import WorldMapper

UP = np.array([0.0, 1.0, 0.0])


def column_near_range(r: np.ndarray, cols: np.ndarray, q_col: float = 0.25, q_near: float = 0.10) -> float | None:
    """Distance to an obstacle's nearest part, robust to depth noise.

    Each image column looks along one bearing, so its points belong to the surface facing the
    user at that bearing: take a low quantile per column (the front face), then a low quantile
    across columns (the part of the obstacle nearest the user). A plain 5th percentile of all
    points is pulled towards the sensor by noise (about 1.6 sigma: ~20 cm on foliage in sun at 4 m).
    """
    order = np.lexsort((r, cols))
    cs, rs = cols[order], r[order]
    starts = np.flatnonzero(np.r_[True, cs[1:] != cs[:-1]])
    counts = np.diff(np.r_[starts, cs.size])
    good = counts >= 3
    if good.sum() < 1:
        return None
    q = np.sort(rs[starts[good] + ((counts[good] - 1) * q_col).astype(np.int64)])
    return float(q[int(q_near * (q.size - 1))])


def parallel_edge(x: np.ndarray, z: np.ndarray, body: float, min_span: float = 1.2, max_wobble: float = 0.3) -> bool:
    """Is this drop-off region a curb beside the path? Its inner edge (the side facing the user)
    must run along the direction of travel for ``min_span`` metres without reaching the body width."""
    if z.max() - z.min() < min_span:
        return False
    right = np.median(x) > 0
    bins = ((z - z.min()) / 0.2).astype(np.int32)
    edges = []
    for b in np.unique(bins):
        xb = x[bins == b]
        if xb.size >= 2:
            edges.append(xb.min() if right else xb.max())
    if len(edges) < 5:
        return False
    e = np.asarray(edges)
    lo, hi = np.percentile(e, (10, 90))
    clear = (lo > body) if right else (hi < -body)
    return bool(hi - lo < max_wobble and clear)


def label8(occ: np.ndarray) -> tuple[np.ndarray, int]:
    """8-connected component labelling of a small boolean grid (no SciPy needed)."""
    H, W = occ.shape
    labels = np.zeros((H, W), np.int32)
    grid = occ.tolist()
    lab = labels.tolist()
    n = 0
    for y0, x0 in zip(*np.nonzero(occ)):
        y0, x0 = int(y0), int(x0)
        if lab[y0][x0]:
            continue
        n += 1
        lab[y0][x0] = n
        stack = [(y0, x0)]
        while stack:
            y, x = stack.pop()
            for ny in (y - 1, y, y + 1):
                if ny < 0 or ny >= H:
                    continue
                row, lrow = grid[ny], lab[ny]
                for nx in (x - 1, x, x + 1):
                    if 0 <= nx < W and row[nx] and not lrow[nx]:
                        lrow[nx] = n
                        stack.append((ny, nx))
    if n:
        labels = np.array(lab, np.int32)
    return labels, n


def mount_matrix(pitch_deg: float) -> np.ndarray:
    """camera (x right, y down, z fwd) -> body (x right, y up, z fwd), sensor pitched by pitch_deg."""
    a = math.radians(-pitch_deg)
    c, s = math.cos(a), math.sin(a)
    rx = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    return (rx @ np.diag([1.0, -1.0, 1.0])).astype(np.float32)


@dataclass
class Obstacle:
    kind: str  # body | head | low | drop | wall
    r: float  # horizontal distance to the nearest relevant point (m)
    az: float  # bearing, degrees, + = right
    x: float
    z: float
    h_min: float
    h_max: float
    x_min: float
    x_max: float
    z_min: float
    z_max: float
    n_points: int
    in_path: bool
    suppressed: bool = False
    full_id: int = 0  # whole-object segment (for recognition; the corridor split may cut objects)
    features: np.ndarray | None = None


@dataclass
class Track:
    id: int
    kind: str
    x: float
    z: float
    r: float
    az: float
    h_min: float
    h_max: float
    width: float
    in_path: bool
    suppressed: bool
    first_seen: float  # capture time of the first frame it appeared in
    last_seen: float
    x_min: float = 0.0
    x_max: float = 0.0
    z_min: float = 0.0
    z_max: float = 0.0
    hits: int = 1
    confirmed: bool = False
    confirmed_at: float | None = None
    vel_r: float = 0.0  # m/s, negative = approaching
    eligible_since: float | None = None  # capture time it first became reportable (latency reference)
    reporting: bool = False
    probs: np.ndarray | None = None  # smoothed class probabilities from the recogniser
    memory: bool = False  # near mode: kept alive after going too close for the sensor to see
    features: np.ndarray | None = None
    frames: int = 1  # frames processed since it first appeared (hits / frames = how steadily it is seen)
    stable_label: str | None = None  # recognised class, with hysteresis so it doesn't flicker
    pend: dict = field(default_factory=dict)  # attribute -> (proposed value, frames seen) awaiting settle

    def settle(self, attr: str, new, frames: int):
        """Change ``attr`` only once ``new`` has been observed ``frames`` frames in a row."""
        if new == getattr(self, attr):
            self.pend.pop(attr, None)
            return
        v, n = self.pend.get(attr, (None, 0))
        n = n + 1 if v == new else 1
        if n >= frames:
            setattr(self, attr, new)
            self.pend.pop(attr, None)
        else:
            self.pend[attr] = (new, n)

    def predicted_xz(self, t: float) -> tuple[float, float]:
        """Where it should be now: moved along its bearing at its closing speed since last seen."""
        dt = t - self.last_seen
        if dt <= 0 or self.r <= 0.05:
            return self.x, self.z
        f = max(0.2, self.r + self.vel_r * dt) / self.r
        return self.x * f, self.z * f

    @property
    def label(self) -> str:
        if self.kind in ("drop",):
            return drop_label(self.h_min, self.width)
        if self.kind == "edge":
            return "curb"
        # geometry already proves these, and it is more reliable than shape when objects touch
        # (a sign fixed to a wall merges with the wall): free space below = overhang, all below
        # knee height = low obstacle
        if self.kind == "head":
            return "overhang"
        if self.kind == "low":
            return "low"
        return self.stable_label or "unknown"

    @property
    def label_conf(self) -> float:
        if self.kind in ("drop", "edge", "head", "low"):
            return 1.0
        if self.probs is None or self.stable_label is None:
            return 0.0
        return float(self.probs[CLASSES.index(self.stable_label)])

    def el(self, ear_height: float) -> float:
        if self.kind == "head":
            h = self.h_min
        elif self.kind in ("low", "drop"):
            h = max(self.h_max, 0.0)
        else:
            h = min(max((self.h_min + self.h_max) / 2, 0.3), 1.9)
        return math.degrees(math.atan2(h - ear_height, max(self.r, 0.3)))

    def ttc(self) -> float | None:
        """Seconds until contact at the current closing speed (None when not closing)."""
        if self.hits >= 5 and self.vel_r < -0.3:
            return self.r / -self.vel_r
        return None

    def urgency_range(self, walk_speed: float = 1.0) -> float:
        """Distance that sounds as urgent as this obstacle is: a person closing at 2.5 m/s from 3 m
        is as urgent as a post 1.2 m ahead of someone walking at 1 m/s."""
        t = self.ttc()
        return self.r if t is None else min(self.r, max(0.3, t * walk_speed))

    def to_dict(self, ear_height: float = 1.6) -> dict:
        return {"id": self.id, "kind": self.kind, "r": round(self.r, 3), "az": round(self.az, 2),
                "el": round(self.el(ear_height), 1), "x": round(self.x, 3), "z": round(self.z, 3),
                "h_min": round(self.h_min, 2), "h_max": round(self.h_max, 2), "width": round(self.width, 2),
                "in_path": self.in_path, "suppressed": self.suppressed, "confirmed": self.confirmed,
                "memory": self.memory, "hits": self.hits,
                "vel_r": round(self.vel_r, 2), "first_seen": self.first_seen, "eligible_since": self.eligible_since,
                "ttc": None if self.ttc() is None else round(self.ttc(), 2), "r_urgent": round(self.urgency_range(), 3),
                "label": self.label, "label_conf": round(self.label_conf, 2),
                "box": [round(self.x_min, 3), round(self.x_max, 3), round(self.z_min, 3), round(self.z_max, 3)]}


@dataclass
class ProcessResult:
    seq: int
    t_capture: float
    tracks: list  # all live tracks (confirmed or not)
    reported: list  # ordered, reportable confirmed tracks (dicts)
    valid_ratio: float
    ground: dict
    timings: dict
    counts: dict = field(default_factory=dict)
    quality: float = 1.0  # share of floor-facing pixels that returned depth (sunlight / dropout indicator)
    world: dict | None = None  # world map pose and status (processing/worldmap.py), None when off


class DepthProcessor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.p = cfg["processing"]
        self._intr_key = None
        self.tracks: list[Track] = []
        self._top_id = None
        self._next_id = 1
        self.plane: np.ndarray | None = None  # (nx, ny, nz, d) with n.p + d = height
        self.plane_age = 0.0
        self.plane_ok = False
        self.plane_auto = False  # the floor was found away from the configured mount (see _ground)
        self.plane_src = "none"  # fit | gravity | none: where the current floor estimate comes from
        self.gravity = GravityEstimator()
        self._walls_up = None  # (up in body frame, time): from two large walls, when no accelerometer
        self._last_height = None  # sensor height above the last well-fitted floor
        self._last_search = -1e9
        self.rng = np.random.default_rng(7)
        self.front = DepthFrontEnd(self.p)
        self.max_seen_depth = 0.0
        self.near_mode = bool(self.p.get("near_mode", False))
        self.depth_stats = {"near": None, "far": None}
        wm = self.p.get("world_map") or {}
        self.mapper = WorldMapper(wm) if wm.get("enabled", False) else None
        self.recognizer = ObjectRecognizer(self.p.get("object_model") or None) if self.p.get("recognition", True) else None
        self.set_sensitivity(cfg.get("default_sensitivity", "normal"))
        self.set_mount(cfg["sensor"]["mount_height"], cfg["sensor"]["mount_pitch_deg"])

    # ---------------------------------------------------------------- setup
    def set_sensitivity(self, level: str):
        levels = self.cfg.get("sensitivity_levels") or {}
        self.sensitivity = level if level in levels else "high"
        self.sens = dict(levels.get(self.sensitivity, {}))
        self.confirm_frames = int(self.p["confirm_frames"]) + int(self.sens.get("confirm_extra", 0))

    def frames_to_confirm(self, fps: float) -> int:
        """Frames from first sight to the first cue at this frame rate (frame count and minimum age)."""
        return max(self.confirm_frames, int(math.ceil(float(self.sens.get("min_age", 0.0)) * fps - 1e-6)) + 1)

    def set_mount(self, height: float, pitch_deg: float):
        self.mount_height = float(height)
        self.mount_pitch = float(pitch_deg)
        self.M = mount_matrix(self.mount_pitch)
        self.plane = None
        self.plane_auto = False
        self._last_height = None
        self._last_search = -1e9
        self._intr_key = None  # floor-facing mask depends on the mount

    def reset(self):
        self.tracks = []
        self._top_id = None
        self.plane = None
        self._last_height = None
        self.gravity.reset()  # a mirrored image or another sensor flips the learned axes
        self._last_search = -1e9
        self.front.reset()
        if self.mapper is not None:
            self.mapper.reset()

    def effective_stride(self, fx: float) -> int:
        """``stride`` is tuned for the Kinect v2 (0.16 deg per pixel). Other sensors get the stride
        that gives the same angular sampling: finer for a wide-angle sensor (so a 3 cm cable is
        still caught), coarser for the Kinect v1's smaller pixels (so it isn't processing 2.6x
        more points than it needs)."""
        ref = float(self.p.get("stride_reference_fx", 365.456))
        return max(1, int(round(int(self.p["stride"]) * fx / ref)))

    def _prepare(self, intr: dict, shape):
        key = (intr["fx"], intr["fy"], intr["cx"], intr["cy"], shape, self.p["stride"], self.front.mode)
        if key == self._intr_key:
            return
        s = self.effective_stride(intr["fx"])
        self.front.stride = s
        self.stride_used = s
        H, W = shape
        h, w = H // s, W // s
        ps = self.front.prestride if s % self.front.prestride == 0 else 1
        off = 0.0 if self.front.mode == "stride" else (s - ps) / 2  # block centres (in full-res pixels)
        v, u = np.mgrid[0:h, 0:w].astype(np.float32)
        u, v = u * s + off, v * s + off
        self.xs = ((u - intr["cx"]) / intr["fx"]).ravel()
        self.ys = ((v - intr["cy"]) / intr["fy"]).ravel()
        self.cols = np.tile(np.arange(w, dtype=np.int32), h)
        self.fx_used = float(intr["fx"])
        self.vfov = math.degrees(2 * math.atan2(H / 2, intr["fy"]))
        self.hfov = math.degrees(2 * math.atan2(W / 2, intr["fx"]))
        # pixels whose ray meets the floor between 0.8 and 3.5 m: with a healthy sensor nearly all of
        # them return depth (floor or whatever stands on it), so their dropout rate measures sensing quality
        M = self.M
        ydir = M[1, 0] * self.xs + M[1, 1] * self.ys + M[1, 2]
        zdir = M[2, 0] * self.xs + M[2, 1] * self.ys + M[2, 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            t = np.where(ydir < -1e-3, -self.mount_height / ydir, np.inf)
        reach = t * zdir
        self.floor_mask = (reach > 0.8) & (reach < 3.5) & (t < 4.3)
        self._intr_key = key

    # ---------------------------------------------------------------- frame
    def process(self, frame: DepthFrame, range_limit: float = 4.0, report_off_path: bool | None = None,
                ear_height: float = 1.6) -> ProcessResult:
        t0 = time.perf_counter()
        p = self.p
        depth = frame.depth
        self._prepare(frame.intrinsics, depth.shape)
        s = self.stride_used
        H0, W0 = depth.shape
        raw = depth[: H0 // s * s: s, : W0 // s * s: s]
        valid_ratio = float((raw > 0).mean()) if raw.size else 0.0  # before any filling: honest for health
        fm = self.floor_mask
        quality = float((raw.ravel()[fm] > 0).mean()) if fm.any() else 1.0
        z = self.front(depth).ravel()
        valid = z > 0
        if valid.any():  # the sensor's effective depth limit, learned from the data (any sensor model)
            zv = z[valid]
            self.depth_stats = {"near": round(float(np.percentile(zv, 0.2)), 3), "far": round(float(np.percentile(zv, 99.8)), 3)}
            zmax = float(zv.max())
            self.max_seen_depth = zmax if zmax > self.max_seen_depth else self.max_seen_depth * 0.9995 + zmax * 0.0005
        zc = z[valid]
        cols = self.cols[valid]
        xc, yc = self.xs[valid] * zc, self.ys[valid] * zc
        M = self.M
        X = M[0, 0] * xc + M[0, 1] * yc + M[0, 2] * zc
        Y = M[1, 0] * xc + M[1, 1] * yc + M[1, 2] * zc + self.mount_height
        Z = M[2, 0] * xc + M[2, 1] * yc + M[2, 2] * zc
        t1 = time.perf_counter()

        self._last_pts = (X, Y, Z)
        acc = frame.extra.get("accel") if frame.extra else None
        if acc is not None:
            self.gravity.update(acc, frame.t_capture)
        hgt = self._ground(X, Y, Z, frame.t_capture)
        tm0 = time.perf_counter()
        if self.mapper is not None:
            normal = self.plane[:3] if self.plane is not None else UP
            self.mapper.update(X, Y, Z, hgt, normal, self._sensor_height(), frame.t_capture,
                               truth=frame.extra.get("pose") if frame.extra else None)
        t2 = time.perf_counter()

        gtol = p["ground_tolerance"]
        top = p["user_height"] + p["head_margin"]
        gw, gd, cell = p["grid_half_width"], p["grid_depth"], p["cell_size"]
        in_grid = (X > -gw) & (X < gw) & (Z > 0) & (Z < gd)
        # a small tilt error in the fitted floor (walking bob, an uneven pavement) lifts the far floor
        # by tilt x distance, and depth noise grows with distance too: widen the floor band with range
        slope = float(self.sens.get("ground_slope", 0.0))
        extra = slope * np.maximum(Z - 2.5, 0.0) if slope > 0 else 0.0
        obst = in_grid & (hgt > gtol + extra) & (hgt < top)
        drop = in_grid & (hgt < -(p["drop_threshold"] + extra))
        ground = np.abs(hgt) <= gtol
        # the walking corridor is clustered on its own so that a wall or parked car beside the path
        # never merges with (and hides the distance of) an obstacle that is actually in the path
        corridor = p["corridor_half_width"] * float(self.sens.get("corridor_scale", 1.0))
        corr = np.abs(X) <= corridor
        rec = self.recognizer is not None  # shape features are computed even before a model is trained
        full_lab = np.zeros(X.size, np.int32)
        if rec and obst.any():
            seg = self._segment(X[obst], Z[obst], cell, gw, gd)
            if seg is not None:
                full_lab[obst] = seg[0]
        self._obst_pts = (X[obst], Z[obst], hgt[obst])  # for the overhang occlusion check
        found = []
        for part, in_path in ((obst & corr, True), (obst & ~corr, False)):
            found += self._clusters(X[part], Z[part], hgt[part], cell, gw, gd, drop=False, in_path=in_path,
                                    cols=cols[part], full=full_lab[part] if rec else None)
        if rec:
            self._describe(found, X, Z, hgt, full_lab)
        # for drop-off points, where the ray crossed the ground plane is the hole's lip
        if drop.any():
            sh = self._sensor_height()
            f = sh / (sh - hgt[drop])
            xd, zd = X[drop] * f, Z[drop] * f
            hd = hgt[drop]
            # curbs / drainage edges running beside the path: found from *all* drop points on each
            # side (so a far, fragmented stretch is still recognised), then left out of clustering
            keep = np.ones(xd.size, bool)
            body = p["body_half_width"]
            for sign in (1.0, -1.0):
                side = sign * xd > body
                if side.sum() >= 20 and parallel_edge(xd[side], zd[side], body):
                    inner = np.percentile(sign * xd[side], 10)
                    band = side & (sign * xd >= inner - 0.15)
                    keep &= ~band
                    zb = zd[band]
                    found.append(Obstacle("edge", float(np.hypot(sign * inner, zb.min())), math.degrees(math.atan2(sign * inner, max(zb.min(), 0.1))),
                                          float(sign * inner), float(zb.min()), float(hd[band].min()), 0.0,
                                          float(min(sign * inner, sign * (inner + 0.4))), float(max(sign * inner, sign * (inner + 0.4))),
                                          float(zb.min()), float(zb.max()), int(band.sum()), False, True))
            xd, zd, hd = xd[keep], zd[keep], hd[keep]
            corr = np.abs(xd) <= corridor
            for part, in_path in ((corr, True), (~corr, False)):
                found += self._clusters(xd[part], zd[part], hd[part], cell, gw, gd, drop=True, in_path=in_path)
        t3 = time.perf_counter()

        self._track(found, frame.t_capture)
        if report_off_path is None:
            report_off_path = bool(p["report_off_path"])
        reported = self._prioritise(range_limit, report_off_path, frame.t_capture)
        t4 = time.perf_counter()

        ms = lambda a, b: round((b - a) * 1000, 2)  # noqa: E731
        return ProcessResult(
            seq=frame.seq, t_capture=frame.t_capture, tracks=list(self.tracks),
            reported=[t.to_dict(ear_height) for t in reported], valid_ratio=valid_ratio, quality=quality,
            ground=self.ground_info(),
            timings={"project": ms(t0, t1), "ground": ms(t1, tm0), "map": ms(tm0, t2), "cluster": ms(t2, t3),
                     "track": ms(t3, t4), "total": ms(t0, t4)},
            world=self.mapper.state() if self.mapper is not None else None,
            counts={"points": int(valid.sum()), "ground": int(ground.sum()), "obstacle": int(obst.sum()),
                    "drop": int(drop.sum())},
        )

    # --------------------------------------------------------------- ground
    def _sensor_height(self) -> float:
        if self.plane is None:
            return self.mount_height
        n, d = self.plane[:3], self.plane[3]
        return float(n[1] * self.mount_height + d)

    def gravity_up(self, t: float) -> np.ndarray | None:
        """Up in the body frame from the accelerometer, once its axes have been verified."""
        u = self.gravity.up_cam(t)
        return None if u is None else self.M @ u

    def level_ref(self, t: float) -> np.ndarray | None:
        """Best known "up" to check floor candidates against: the accelerometer, else recent walls."""
        g = self.gravity_up(t)
        if g is not None:
            return g
        if self._walls_up is not None and t - self._walls_up[1] < 3.0:
            return self._walls_up[0]
        return None

    def _ground(self, X, Y, Z, t):
        p = self.p
        band = p["ground_search_band"]
        g_up = self.gravity_up(t)
        level = self.level_ref(t)
        # search near the floor we are already tracking (or, before that, the nominal mount): once
        # the floor has been found somewhere unexpected it is followed from there
        href = Y if self.plane is None else self.plane[0] * X + self.plane[1] * Y + self.plane[2] * Z + self.plane[3]
        # the floor the user walks on is the one under the corridor; a street or step beside the
        # sidewalk must not win the fit just because it fills more of the image
        cand = (np.abs(href) < band) & (Z > 0.3) & (Z < 4.0) & (np.abs(X) < p["corridor_half_width"] + 0.35)
        idx = np.flatnonzero(cand)
        fitted = None
        if idx.size >= 150:
            P = np.stack([X[idx], Y[idx], Z[idx]], 1).astype(np.float64)
            fitted = self._ransac(P, p["ransac_iterations"], p["ransac_threshold"], level)
        if fitted is None and (self.plane is None or t - self.plane_age > 0.5) and t - self._last_search >= 0.4:
            # the sensor is not where the configuration says (on a desk, held in the hand, strapped
            # higher or tilted more): without this the whole floor would be reported as an obstacle
            # or a drop-off, all the time. Look for the floor anywhere in view and follow it.
            self._last_search = t
            g = self._find_floor(X, Y, Z, n_sub=1500, iters=300, g_up=g_up, t=t)
            if g is not None:
                fitted = g[0]
                self.plane = None  # adopt it outright, don't blend with a floor that was wrong
                if not self.plane_auto:
                    self.plane_auto = True
        if fitted is not None:
            a = p["ground_smoothing"] if self.plane is not None and self.plane_src == "fit" else 1.0
            plane = fitted if self.plane is None else (1 - a) * self.plane + a * fitted
            plane[:3] /= np.linalg.norm(plane[:3])
            self.plane, self.plane_age, self.plane_ok, self.plane_src = plane, t, True, "fit"
            self._last_height = self._sensor_height()
            if self.gravity.acc is not None:
                self.gravity.learn_from_floor(self.M.T @ fitted[:3])
        elif self.level_ref(t) is not None and (self.plane is None or t - self.plane_age > 0.3):
            # no floor in view (facing a wall up close, a crowd, a covered floor, a sensor on a desk
            # looking level): level from gravity, or from the walls, at the height the floor was last
            # seen. Before, it fell back to the configured mount after 2 s, and a mount that is off by
            # 20 deg turns the far end of the room into "holes".
            lv = self.level_ref(t)
            h = self._last_height if self._last_height is not None else self.mount_height
            self.plane = np.array([*lv, h - lv[1] * self.mount_height])
            self.plane_ok, self.plane_src = False, ("gravity" if g_up is not None else "walls")
        elif self.plane is not None and t - self.plane_age > 2.0:
            self.plane, self.plane_ok, self.plane_src = None, False, "none"  # stale: fall back to the nominal mount
        if self.plane is None:
            self.plane_ok = fitted is not None
            return Y
        n, d = self.plane[:3], self.plane[3]
        return n[0] * X + n[1] * Y + n[2] * Z + d

    def _walls_up_from(self, sub, nrm, ds, counts):
        """Indoors, the biggest surfaces are often walls, and walls are vertical: two walls that aren't
        parallel give "up" as the direction along both (no floor or accelerometer needed). Returns the
        up vector (body frame) or None."""
        wall = np.abs(nrm[:, 1]) < np.sin(np.radians(40))  # far from the configured "up": vertical-ish
        if wall.sum() < 2:
            return None
        order = np.argsort(-np.where(wall, counts, -1))
        i1 = order[0]
        n1, c1 = nrm[i1], counts[i1]
        if c1 < 0.15 * sub.shape[0]:
            return None
        on1 = np.abs(sub @ n1 + ds[i1]) < 0.03
        for j in order[1:]:
            if not wall[j] or counts[j] < 0.08 * sub.shape[0]:
                break
            n2 = nrm[j]
            # a different wall: not parallel, and mostly its own points (not a sliver of the first one)
            own = (np.abs(sub @ n2 + ds[j]) < 0.03) & ~on1
            if abs(float(n1 @ n2)) < np.cos(np.radians(30)) and own.sum() >= 0.08 * sub.shape[0]:
                u = np.cross(n1, n2)
                u /= max(np.linalg.norm(u), 1e-9)
                u *= np.sign(u[1] + 1e-12)
                return u if u[1] > np.cos(np.radians(45)) else None
        return None

    def _find_floor(self, X, Y, Z, n_sub: int = 3000, iters: int = 600, seed: int | None = None, g_up=None,
                    t: float | None = None):
        """The floor anywhere in view, assuming nothing about mount height or tilt: of the large level
        surfaces, the lowest (a table top is level too, but higher). "Level" is checked against the
        accelerometer when there is one, else against the walls when two are in view. Returns
        ((n, d) plane, inlier count), or None: better no floor than a sloping chair seat taken for one,
        which turns everything beyond it into holes."""
        m = (Z > 0.3) & (Z < 4.5)
        if m.sum() < 300:
            return None
        P = np.stack([X[m], Y[m], Z[m]], 1).astype(np.float64)
        rng = self.rng if seed is None else np.random.default_rng(seed)
        sub = P[rng.choice(P.shape[0], min(P.shape[0], n_sub), replace=False)]
        tri = sub[rng.integers(0, sub.shape[0], (iters, 3))]
        nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(nrm, axis=1)
        ok = ln > 1e-9
        nrm, tri = nrm[ok] / ln[ok, None], tri[ok]
        nrm *= np.sign(nrm[:, 1:2] + 1e-12)
        ds_all = -(nrm * tri[:, 0]).sum(1)
        counts_all = (np.abs(sub @ nrm.T + ds_all) < 0.03).sum(0)
        if g_up is None:
            wu = self._walls_up_from(sub, nrm, ds_all, counts_all)
            if wu is not None and t is not None:
                self._walls_up = (wu, t)
            g_up = wu
        # tolerate a badly guessed tilt; with an accelerometer or walls, only surfaces that really are level
        up = (nrm[:, 1] > np.cos(np.radians(35))) if g_up is None else (nrm @ g_up > np.cos(np.radians(12)))
        nrm, ds, counts = nrm[up], ds_all[up], counts_all[up]
        if not len(nrm):
            return None
        heights = nrm[:, 1] * self.mount_height + ds  # sensor height above each candidate plane
        sane = (heights > 0.2) & (heights < 2.5)
        if not sane.any():
            return None
        top = int(np.argmax(np.where(sane, counts, -1)))  # the dominant level surface sets "up"
        if counts[top] < max(60, 0.12 * sub.shape[0]):
            return None
        parallel = (nrm @ nrm[top]) > np.cos(np.radians(6))
        # of the parallel surfaces, a clearly lower one (a desk top above the floor) is the floor; slices
        # of the same floor a few cm apart (Kinect v1 depth terraces, noise) are not separate surfaces
        lower = sane & parallel & (counts >= max(60, 0.3 * counts[top])) & (heights > heights[top] + 0.10)
        best = int(np.argmax(np.where(lower, heights, -1))) if lower.any() else top
        n, d = nrm[best], ds[best]
        inl = P[:0]
        for thr in (0.06, 0.04, 0.03):  # refine: least squares on the inliers, tightening
            inl = P[np.abs(P @ n + d) < thr]
            if inl.shape[0] < 100:
                return None
            c = inl.mean(0)
            _, _, vt = np.linalg.svd(inl - c, full_matrices=False)
            n = vt[2] * (1 if vt[2][1] > 0 else -1)
            d = -float(n @ c)
        return np.array([*n, d]), int(inl.shape[0])

    def _ransac(self, P: np.ndarray, iters: int, thr: float, g_up=None):
        n = P.shape[0]
        sub = P[self.rng.choice(n, min(n, 600), replace=False)]
        tri = sub[self.rng.integers(0, sub.shape[0], (iters, 3))]
        normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        norm = np.linalg.norm(normals, axis=1)
        ok = norm > 1e-9
        if not ok.any():
            return None
        normals = normals[ok] / norm[ok, None]
        normals *= np.sign(normals[:, 1:2] + 1e-12)
        # level enough to be the floor: within 20 deg of the mount's "up", or with a verified accelerometer
        # within 15 deg of gravity (a ramp is fine, a table edge or a car bonnet is not)
        upright = (normals[:, 1] > math.cos(math.radians(20))) if g_up is None else (normals @ g_up > math.cos(math.radians(15)))
        if not upright.any():
            return None
        normals, tri = normals[upright], tri[ok][upright]
        ds = -(normals * tri[:, 0]).sum(1)
        dist = np.abs(sub @ normals.T + ds)
        counts = (dist < thr).sum(0)
        # prefer the surface the sensor is already tracking (or the nominal mount height): a street
        # 15 cm below the sidewalk is a different plane, not a better fit of the same one
        ref_h = self._sensor_height()
        near_ref = np.abs((normals[:, 1] * self.mount_height + ds) - ref_h) < 0.07
        need = max(60, 0.3 * sub.shape[0])
        if (near_ref & (counts >= need)).any():
            counts = np.where(near_ref, counts, -1)
        best = int(np.argmax(counts))
        if counts[best] < need:
            return None
        nb, db = normals[best], ds[best]
        inl = P[np.abs(P @ nb + db) < thr]
        if inl.shape[0] < 50:
            return None
        c = inl.mean(0)
        _, _, vt = np.linalg.svd(inl - c, full_matrices=False)
        nrm = vt[2] * (1 if vt[2][1] > 0 else -1)
        if (nrm[1] if g_up is None else float(nrm @ g_up)) < math.cos(math.radians(20 if g_up is None else 15)):
            return None
        return np.array([*nrm, -float(nrm @ c)])

    def calibrate_floor(self) -> dict | None:
        """Find the floor anywhere in view (no assumption about mount height or tilt) and return the
        sensor height and tilt it implies, or None. Used by "Calibrate from floor": the device may sit
        on a desk, a tripod or a chest strap."""
        pts = getattr(self, "_last_pts", None)
        if pts is None:
            return None
        g = self._find_floor(*pts, seed=1)
        if g is None:
            return None
        (nx, ny, nz, d), inliers = g
        height = float(ny * self.mount_height + d)
        pitch = self.mount_pitch + float(np.degrees(np.arctan2(nz, ny)))
        return {"height": round(height, 3), "pitch": round(pitch, 2), "inliers": inliers}

    def ground_info(self) -> dict:
        if self.plane is None:
            return {"ok": False, "height": self.mount_height, "pitch": self.mount_pitch, "source": "none",
                    "plane": [0.0, 1.0, 0.0, 0.0]}
        n = self.plane[:3]
        # a sensor pitched further down than nominal sees the floor tilted towards it
        pitch_err = math.degrees(math.atan2(n[2], n[1]))
        return {"ok": self.plane_ok, "height": round(self._sensor_height(), 3), "source": self.plane_src,
                "pitch": round(self.mount_pitch + pitch_err, 2), "plane": [round(float(v), 5) for v in self.plane]}

    # ------------------------------------------------------------- clusters
    def _segment(self, X, Z, cell, gw, gd):
        """Connected top-down grid segments of a point set: (label per point, number of segments)."""
        nx, nz = int(round(2 * gw / cell)), int(round(gd / cell))
        ix = np.clip(((X + gw) / cell).astype(np.int32), 0, nx - 1)
        iz = np.clip((Z / cell).astype(np.int32), 0, nz - 1)
        flat = iz * nx + ix
        counts = np.bincount(flat, minlength=nx * nz).reshape(nz, nx)
        zrow = (np.arange(nz) + 0.5) * cell
        thr = np.maximum(2, np.round(self.p["min_cell_points"] * np.minimum(1.0, 2.0 / np.maximum(zrow, 0.5)))).astype(int)
        labels, nlab = label8(counts >= thr[:, None])
        if nlab == 0:
            return None
        return labels.ravel()[flat], nlab

    def _hidden_below(self, x_min, x_max, z_min, z_max, h_min) -> bool:
        """Is the space under this obstacle hidden by something lower standing in front of it or under it?"""
        X, Z, Hh = getattr(self, "_obst_pts", (None, None, None))
        if X is None or X.size == 0:
            return False
        m = ((X >= x_min - 0.1) & (X <= x_max + 0.1) & (Z >= z_min - 0.8) & (Z <= z_max + 0.05)
             & (Hh < h_min - 0.25))
        return int(m.sum()) >= 5

    def _seen_below(self, r: float, margin: float = 0.0) -> float:
        """Lowest height the sensor sees at horizontal distance r: limited by the bottom edge of the
        field of view and, far away, by the depth limit (measured along the tilted optical axis, so
        the bottom of a distant object goes out of range before its top)."""
        pitch = self.ground_info()["pitch"]
        H = self._sensor_height()
        lo = math.radians(pitch - getattr(self, "vfov", 60.0) / 2)
        seen = H + r * math.tan(lo)
        a = math.radians(-pitch)
        if a > 0.01 and self.max_seen_depth > 0:
            seen = max(seen, H - (self.max_seen_depth - margin - r * math.cos(a)) / math.sin(a))
        return max(0.0, seen)

    def _describe(self, found, X, Z, hgt, full_lab):
        """Shape features of each whole object an obstacle belongs to."""
        p = self.p
        cache = {}
        top = p["user_height"] + p["head_margin"]
        sh = self._sensor_height()
        g = self.ground_info()
        for ob in found:
            if ob.kind in ("drop", "edge") or ob.full_id <= 0:
                continue
            if ob.full_id not in cache:
                m = full_lab == ob.full_id
                x, z, h = X[m], Z[m], hgt[m]
                r = np.hypot(x, z)
                cache[ob.full_id] = shape_features(x, z, h, r, float(np.percentile(r, 5)), self.stride_used,
                                                   self.fx_used, sh, g["pitch"], self.vfov, top,
                                                   hfov_deg=self.hfov) if m.sum() >= 6 else None
            ob.features = cache[ob.full_id]

    def _clusters(self, X, Z, Hh, cell, gw, gd, drop: bool, in_path: bool, cols=None, full=None) -> list[Obstacle]:
        p = self.p
        min_pts = max(p["min_cluster_points"], int(round(p["min_cluster_points"] * float(self.sens.get("cluster_scale", 1.0)))))
        if X.size < min_pts:
            return []
        nx, nz = int(round(2 * gw / cell)), int(round(gd / cell))
        ix = np.clip(((X + gw) / cell).astype(np.int32), 0, nx - 1)
        iz = np.clip((Z / cell).astype(np.int32), 0, nz - 1)
        flat = iz * nx + ix
        counts = np.bincount(flat, minlength=nx * nz).reshape(nz, nx)
        # far surfaces get fewer pixels per cell; relax the threshold with range
        zrow = (np.arange(nz) + 0.5) * cell
        thr = np.maximum(2, np.round(p["min_cell_points"] * np.minimum(1.0, 2.0 / np.maximum(zrow, 0.5)))).astype(int)
        occ = counts >= thr[:, None]
        labels, nlab = label8(occ)
        if nlab == 0:
            return []
        plab = labels.ravel()[flat]
        order = np.argsort(plab, kind="stable")
        plab_s = plab[order]
        bounds = np.searchsorted(plab_s, np.arange(1, nlab + 2))
        body = p["body_half_width"]
        out = []
        for k in range(nlab):
            sel = order[bounds[k]:bounds[k + 1]]
            if sel.size < min_pts:
                continue
            x, zz, h = X[sel], Z[sel], Hh[sel]
            r = np.hypot(x, zz)
            x_min, x_max = np.percentile(x, (2, 98))
            r_near = None
            if cols is not None and p.get("column_estimator", True):
                r_near = column_near_range(r, cols[sel])
            if r_near is None:
                kth = max(0, int(0.05 * r.size) - 1)
                r_near = float(np.partition(r, kth)[kth])
            near = r <= r_near + 0.15
            xn, zn = float(np.median(x[near])), float(np.median(zz[near]))
            z_min, z_max = np.percentile(zz, (2, 98))
            # height band from the obstacle's front face only: mixed/flying pixels behind an edge
            # would otherwise drag h_min to the floor and hide an overhang
            face = r <= r_near + 0.35
            h_min, h_max = np.percentile(h[face], (3, 97)) if face.sum() >= 4 else np.percentile(h, (3, 97))
            if drop:
                kind = "drop"
                h_min, h_max = float(h.min()), 0.0
            elif (h_min >= p["overhang_min_height"] and self._seen_below(r_near, margin=0.3) < h_min - 0.25
                  and not self._hidden_below(x_min, x_max, z_min, z_max, h_min)):
                # free space below is only proven if the sensor actually saw below it: up close the
                # bottom of the field of view cuts off the lower half of a tricycle, far away the depth
                # limit cuts off the bottom of a wall, and the top of a parked motorcycle hides the
                # space under its own far end. None of those is an overhang.
                kind = "head"
            elif h_max <= p["low_max_height"]:
                kind = "low"
            else:
                kind = "body"
            suppressed = False
            if drop and not suppressed and parallel_edge(x, zz, body):
                kind, suppressed = "edge", True  # curb / drop-off edge running beside the path
            elif (z_max - z_min) > 1.2 and (x_max - x_min) < 0.4 and not (x_min <= body and x_max >= -body):
                # a wall, or a curb / drainage edge, running parallel to the direction of travel:
                # it guides the user rather than blocking them, so it is not cued
                kind, suppressed = ("edge" if drop else "wall"), True
            ob = Obstacle(kind, r_near, math.degrees(math.atan2(xn, zn)), xn, zn, float(h_min), float(h_max),
                          float(x_min), float(x_max), float(z_min), float(z_max), int(sel.size), in_path, suppressed)
            if full is not None:
                fl = full[sel]
                fl = fl[fl > 0]
                ob.full_id = int(np.bincount(fl).argmax()) if fl.size else 0
            out.append(ob)
        return out

    # -------------------------------------------------------------- tracker
    def _track(self, found: list[Obstacle], t: float):
        p = self.p
        gate, alpha = p["assoc_gate"], p["smoothing"]
        need, min_age = self.confirm_frames, float(self.sens.get("min_age", 0.0))
        ratio = float(self.sens.get("hit_ratio", 0.0))
        for tr in self.tracks:
            if not tr.memory:
                tr.frames += 1
        pairs = []
        for i, tr in enumerate(self.tracks):
            px, pz = tr.predicted_xz(t)
            for j, ob in enumerate(found):
                if (tr.kind in ("drop", "edge")) != (ob.kind in ("drop", "edge")):
                    continue
                d = math.hypot(px - ob.x, pz - ob.z)
                if d < gate:
                    pairs.append((d, i, j))
        pairs.sort()
        used_t, used_o = set(), set()
        for _d, i, j in pairs:
            if i in used_t or j in used_o:
                continue
            used_t.add(i)
            used_o.add(j)
            self._update(self.tracks[i], found[j], t, need, min_age, ratio)
        for j, ob in enumerate(found):
            if j in used_o:
                continue
            tr = Track(self._next_id, ob.kind, ob.x, ob.z, ob.r, ob.az, ob.h_min, ob.h_max, ob.x_max - ob.x_min,
                       ob.in_path, ob.suppressed, t, t, ob.x_min, ob.x_max, ob.z_min, ob.z_max)
            self._recognise(tr, ob)
            if need <= 1 and min_age <= 0:
                tr.confirmed, tr.confirmed_at = True, t
            self._next_id += 1
            self.tracks.append(tr)
        keep = []
        for tr in self.tracks:
            age = t - tr.last_seen
            if age <= p["drop_time"]:
                if age == 0:
                    tr.memory = False
                keep.append(tr)
            elif self.near_mode and self._too_close_to_see(tr) and age <= p["near_hold_time"]:
                # near mode: it was in the path, close and confirmed, and vanished because it went
                # inside the sensor's minimum range (~0.45 m on a Kinect v1). Keep warning instead
                # of going silent, closing in at its last speed (at least a slow step).
                tr.memory = True
                tr.r = max(0.2, tr.r + min(tr.vel_r, -0.2) * (t - getattr(tr, "_mem_t", tr.last_seen)))
                tr._mem_t = t
                keep.append(tr)
        self.tracks = keep

    def _update(self, tr: Track, ob: Obstacle, t: float, need: int, min_age: float, ratio: float):
        p = self.p
        alpha = p["smoothing"]
        dt = max(t - tr.last_seen, 1e-3)
        # alpha-beta filter on range: smooths noise without lagging behind something that moves
        pred = max(0.0, tr.r + tr.vel_r * dt)
        res = ob.r - pred
        if ob.r < pred - 0.3:
            tr.r = ob.r  # suddenly much closer: take it at once, safety first
        else:
            tr.r = pred + alpha * res
        tr.vel_r = float(np.clip(tr.vel_r + p["velocity_gain"] * res / dt, -5.0, 5.0))
        tr.az = (1 - alpha) * tr.az + alpha * ob.az
        tr.x, tr.z = ob.x, ob.z
        tr.h_min, tr.h_max = ob.h_min, ob.h_max
        tr.width = ob.x_max - ob.x_min
        tr.x_min, tr.x_max, tr.z_min, tr.z_max = ob.x_min, ob.x_max, ob.z_min, ob.z_max
        # changes that make it more of a hazard apply at once (it enters the path, it stops looking
        # like a wall beside the path); changes that would quiet it or alter its sound must hold for a
        # few frames, so the pitch and the cue don't flicker as the shape wobbles
        tr.settle("in_path", ob.in_path, 1 if ob.in_path else 3)
        tr.settle("suppressed", ob.suppressed, 3 if ob.suppressed else 1)
        if ob.kind != "drop":
            tr.settle("kind", ob.kind, 2 if ob.kind == "head" else 3)
        self._recognise(tr, ob)
        tr.last_seen = t
        tr.hits += 1
        if tr.confirmed:
            return
        if tr.hits >= need and t - tr.first_seen >= min_age - 1e-6 and tr.hits >= ratio * tr.frames:
            tr.confirmed, tr.confirmed_at = True, t

    def _too_close_to_see(self, tr: Track) -> bool:
        p = self.p
        return (tr.confirmed and tr.in_path and not tr.suppressed and tr.kind not in ("drop", "edge")
                and (tr.r < p["near_hold_distance"] or tr.memory))

    def _recognise(self, tr: Track, ob: Obstacle):
        if ob.features is None or self.recognizer is None:
            return
        tr.features = ob.features
        pr = self.recognizer.proba(ob.features)
        if pr is None:
            return
        tr.probs = pr if tr.probs is None else 0.8 * tr.probs + 0.2 * pr  # labels settle over ~5 frames
        if tr.hits < 4:
            return
        # hysteresis: name it once the evidence is clear, and only rename it when another class is
        # clearly more likely, so "person" doesn't turn into "pole" and back as the shape wobbles
        i = int(np.argmax(tr.probs))
        best = CLASSES[i]
        need = float(self.p.get("label_min_conf", 0.5))  # below this it is just "Obstacle": better unnamed than misnamed
        if tr.stable_label is None:
            if tr.probs[i] >= need:
                tr.stable_label = best
        elif best != tr.stable_label:
            cur = float(tr.probs[CLASSES.index(tr.stable_label)])
            if tr.probs[i] >= need and tr.probs[i] >= cur + 0.2:
                tr.stable_label = best
            elif cur < 0.25:
                tr.stable_label = None  # no longer anything in particular

    def _prioritise(self, range_limit: float, report_off_path: bool, t: float) -> list[Track]:
        bonus = self.p["priority_bonus"]
        cands = []
        for tr in self.tracks:
            # hysteresis: once reported, keep reporting until clearly out of range (no flicker at the edge)
            lim = range_limit + (0.15 if tr.reporting else 0.0)
            eligible = not tr.suppressed and tr.r <= lim and (tr.in_path or report_off_path)
            if eligible and tr.eligible_since is None:
                tr.eligible_since = t
            elif not eligible:
                tr.eligible_since = None
            tr.reporting = eligible and tr.confirmed
            if tr.reporting:
                cands.append(tr)

        sticky = float(self.p.get("rank_hysteresis", 0.3))

        def key(tr: Track):
            eff = tr.urgency_range() - (bonus if tr.kind in ("head", "drop") else 0.0)
            # the obstacle sounding first keeps that place until another is clearly more urgent:
            # two obstacles at similar distances must not trade the lead cue every few frames
            if tr.id == self._top_id:
                eff -= sticky
            return (0 if tr.in_path else 1, eff)

        out = sorted(cands, key=key)
        self._top_id = out[0].id if out else None
        return out
