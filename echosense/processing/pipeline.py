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
   be seen in ``confirm_frames`` consecutive-ish frames before it is reported),
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
from .objects import CLASSES, ObjectRecognizer, drop_label, shape_features

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
        if self.probs is None or self.hits < 3:
            return "unknown"
        i = int(np.argmax(self.probs))
        return CLASSES[i] if self.probs[i] >= 0.45 else "unknown"

    @property
    def label_conf(self) -> float:
        if self.kind in ("drop", "edge", "head", "low"):
            return 1.0
        return 0.0 if self.probs is None else float(self.probs.max())

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
                "memory": self.memory,
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


class DepthProcessor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.p = cfg["processing"]
        self._intr_key = None
        self.tracks: list[Track] = []
        self._next_id = 1
        self.plane: np.ndarray | None = None  # (nx, ny, nz, d) with n.p + d = height
        self.plane_age = 0.0
        self.plane_ok = False
        self.rng = np.random.default_rng(7)
        self.front = DepthFrontEnd(self.p)
        self.max_seen_depth = 0.0
        self.near_mode = bool(self.p.get("near_mode", False))
        self.depth_stats = {"near": None, "far": None}
        self.recognizer = ObjectRecognizer(self.p.get("object_model") or None) if self.p.get("recognition", True) else None
        self.set_mount(cfg["sensor"]["mount_height"], cfg["sensor"]["mount_pitch_deg"])

    # ---------------------------------------------------------------- setup
    def set_mount(self, height: float, pitch_deg: float):
        self.mount_height = float(height)
        self.mount_pitch = float(pitch_deg)
        self.M = mount_matrix(self.mount_pitch)
        self.plane = None
        self._intr_key = None  # floor-facing mask depends on the mount

    def reset(self):
        self.tracks = []
        self.plane = None
        self.front.reset()

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
        hgt = self._ground(X, Y, Z, frame.t_capture)
        t2 = time.perf_counter()

        gtol = p["ground_tolerance"]
        top = p["user_height"] + p["head_margin"]
        gw, gd, cell = p["grid_half_width"], p["grid_depth"], p["cell_size"]
        in_grid = (X > -gw) & (X < gw) & (Z > 0) & (Z < gd)
        obst = in_grid & (hgt > gtol) & (hgt < top)
        drop = in_grid & (hgt < -p["drop_threshold"])
        ground = np.abs(hgt) <= gtol
        # the walking corridor is clustered on its own so that a wall or parked car beside the path
        # never merges with (and hides the distance of) an obstacle that is actually in the path
        corr = np.abs(X) <= p["corridor_half_width"]
        rec = self.recognizer is not None  # shape features are computed even before a model is trained
        full_lab = np.zeros(X.size, np.int32)
        if rec and obst.any():
            seg = self._segment(X[obst], Z[obst], cell, gw, gd)
            if seg is not None:
                full_lab[obst] = seg[0]
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
            corr = np.abs(xd) <= p["corridor_half_width"]
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
            timings={"project": ms(t0, t1), "ground": ms(t1, t2), "cluster": ms(t2, t3), "track": ms(t3, t4),
                     "total": ms(t0, t4)},
            counts={"points": int(valid.sum()), "ground": int(ground.sum()), "obstacle": int(obst.sum()),
                    "drop": int(drop.sum())},
        )

    # --------------------------------------------------------------- ground
    def _sensor_height(self) -> float:
        if self.plane is None:
            return self.mount_height
        n, d = self.plane[:3], self.plane[3]
        return float(n[1] * self.mount_height + d)

    def _ground(self, X, Y, Z, t):
        p = self.p
        band = p["ground_search_band"]
        # the floor the user walks on is the one under the corridor; a street or step beside the
        # sidewalk must not win the fit just because it fills more of the image
        cand = (np.abs(Y) < band) & (Z > 0.3) & (Z < 4.0) & (np.abs(X) < p["corridor_half_width"] + 0.35)
        idx = np.flatnonzero(cand)
        fitted = None
        if idx.size >= 150:
            P = np.stack([X[idx], Y[idx], Z[idx]], 1).astype(np.float64)
            fitted = self._ransac(P, p["ransac_iterations"], p["ransac_threshold"])
        if fitted is not None:
            a = p["ground_smoothing"] if self.plane is not None else 1.0
            plane = fitted if self.plane is None else (1 - a) * self.plane + a * fitted
            plane[:3] /= np.linalg.norm(plane[:3])
            self.plane, self.plane_age, self.plane_ok = plane, t, True
        elif self.plane is not None and t - self.plane_age > 2.0:
            self.plane, self.plane_ok = None, False  # stale: fall back to the nominal mount
        if self.plane is None:
            self.plane_ok = fitted is not None
            return Y
        n, d = self.plane[:3], self.plane[3]
        return n[0] * X + n[1] * Y + n[2] * Z + d

    def _ransac(self, P: np.ndarray, iters: int, thr: float):
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
        upright = normals[:, 1] > math.cos(math.radians(20))
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
        if nrm[1] < math.cos(math.radians(20)):
            return None
        return np.array([*nrm, -float(nrm @ c)])

    def calibrate_floor(self) -> dict | None:
        """Find the floor anywhere in view (no assumption about mount height or tilt) and return the
        sensor height and tilt it implies, or None. Used by "Calibrate from floor": the device may sit
        on a desk, a tripod or a chest strap. Of the large level surfaces, the lowest is the floor
        (a table top is level too, but higher)."""
        pts = getattr(self, "_last_pts", None)
        if pts is None:
            return None
        X, Y, Z = pts
        m = (Z > 0.3) & (Z < 4.5) & (Y < self.mount_height + 0.2)
        P = np.stack([X[m], Y[m], Z[m]], 1).astype(np.float64)
        if P.shape[0] < 300:
            return None
        rng = np.random.default_rng(1)
        sub = P[rng.choice(P.shape[0], min(P.shape[0], 3000), replace=False)]
        tri = sub[rng.integers(0, sub.shape[0], (600, 3))]
        nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        ln = np.linalg.norm(nrm, axis=1)
        ok = ln > 1e-9
        nrm, tri = nrm[ok] / ln[ok, None], tri[ok]
        nrm *= np.sign(nrm[:, 1:2] + 1e-12)
        up = nrm[:, 1] > np.cos(np.radians(35))  # tolerate a badly guessed tilt
        nrm, tri = nrm[up], tri[up]
        if not len(nrm):
            return None
        ds = -(nrm * tri[:, 0]).sum(1)
        counts = (np.abs(sub @ nrm.T + ds) < 0.03).sum(0)
        heights = nrm[:, 1] * self.mount_height + ds  # sensor height above each candidate plane
        sane = (heights > 0.2) & (heights < 2.5)
        if not sane.any():
            return None
        top = int(np.argmax(np.where(sane, counts, -1)))  # the dominant level surface sets "up"
        parallel = (nrm @ nrm[top]) > np.cos(np.radians(6))
        big = sane & parallel & (counts >= max(150, 0.3 * counts[top]))
        best = int(np.argmax(np.where(big, heights, -1)))  # of those parallel surfaces, the lowest is the floor
        inl = P[np.abs(P @ nrm[best] + ds[best]) < 0.03]
        c = inl.mean(0)
        _, _, vt = np.linalg.svd(inl - c, full_matrices=False)
        n = vt[2] * (1 if vt[2][1] > 0 else -1)
        d = -float(n @ c)
        height = float(n[1] * self.mount_height + d)
        pitch = self.mount_pitch + float(np.degrees(np.arctan2(n[2], n[1])))
        return {"height": round(height, 3), "pitch": round(pitch, 2), "inliers": int(inl.shape[0])}

    def ground_info(self) -> dict:
        if self.plane is None:
            return {"ok": False, "height": self.mount_height, "pitch": self.mount_pitch,
                    "plane": [0.0, 1.0, 0.0, 0.0]}
        n = self.plane[:3]
        # a sensor pitched further down than nominal sees the floor tilted towards it
        pitch_err = math.degrees(math.atan2(n[2], n[1]))
        return {"ok": self.plane_ok, "height": round(self._sensor_height(), 3),
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

    def _seen_below(self, r: float) -> float:
        """Lowest height the sensor sees at horizontal distance r: limited by the bottom edge of the
        field of view and, far away, by the depth limit (measured along the tilted optical axis, so
        the bottom of a distant object goes out of range before its top)."""
        pitch = self.ground_info()["pitch"]
        H = self._sensor_height()
        lo = math.radians(pitch - getattr(self, "vfov", 60.0) / 2)
        seen = H + r * math.tan(lo)
        a = math.radians(-pitch)
        if a > 0.01 and self.max_seen_depth > 0:
            seen = max(seen, H - (self.max_seen_depth - r * math.cos(a)) / math.sin(a))
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
                                                   self.fx_used, sh, g["pitch"], self.vfov, top) if m.sum() >= 6 else None
            ob.features = cache[ob.full_id]

    def _clusters(self, X, Z, Hh, cell, gw, gd, drop: bool, in_path: bool, cols=None, full=None) -> list[Obstacle]:
        p = self.p
        if X.size < p["min_cluster_points"]:
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
            if sel.size < p["min_cluster_points"]:
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
            elif h_min >= p["overhang_min_height"] and self._seen_below(r_near) < h_min - 0.25:
                # free space below is only proven if the sensor actually saw below it: up close the
                # bottom of the field of view cuts off the lower half of a tricycle, which must not
                # be mistaken for an overhang
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
        pairs = []
        for i, tr in enumerate(self.tracks):
            for j, ob in enumerate(found):
                if (tr.kind in ("drop", "edge")) != (ob.kind in ("drop", "edge")):
                    continue
                d = math.hypot(tr.x - ob.x, tr.z - ob.z)
                if d < gate:
                    pairs.append((d, i, j))
        pairs.sort()
        used_t, used_o = set(), set()
        for _d, i, j in pairs:
            if i in used_t or j in used_o:
                continue
            used_t.add(i)
            used_o.add(j)
            tr, ob = self.tracks[i], found[j]
            dt = max(t - tr.last_seen, 1e-3)
            # alpha-beta filter on range: smooths noise without lagging behind something that moves
            pred = tr.r + tr.vel_r * dt
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
            tr.in_path, tr.suppressed = ob.in_path, ob.suppressed
            self._recognise(tr, ob)
            if tr.kind != ob.kind and ob.kind != "drop":
                tr.kind = ob.kind
            tr.last_seen = t
            tr.hits += 1
            if not tr.confirmed and tr.hits >= p["confirm_frames"]:
                tr.confirmed, tr.confirmed_at = True, t
        for j, ob in enumerate(found):
            if j in used_o:
                continue
            tr = Track(self._next_id, ob.kind, ob.x, ob.z, ob.r, ob.az, ob.h_min, ob.h_max, ob.x_max - ob.x_min,
                       ob.in_path, ob.suppressed, t, t, ob.x_min, ob.x_max, ob.z_min, ob.z_max)
            self._recognise(tr, ob)
            if p["confirm_frames"] <= 1:
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
        tr.probs = pr if tr.probs is None else 0.75 * tr.probs + 0.25 * pr  # labels settle over ~4 frames

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

        def key(tr: Track):
            eff = tr.urgency_range() - (bonus if tr.kind in ("head", "drop") else 0.0)
            return (0 if tr.in_path else 1, eff)

        return sorted(cands, key=key)
