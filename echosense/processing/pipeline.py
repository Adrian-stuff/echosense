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

UP = np.array([0.0, 1.0, 0.0])


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

    def el(self, ear_height: float) -> float:
        if self.kind == "head":
            h = self.h_min
        elif self.kind in ("low", "drop"):
            h = max(self.h_max, 0.0)
        else:
            h = min(max((self.h_min + self.h_max) / 2, 0.3), 1.9)
        return math.degrees(math.atan2(h - ear_height, max(self.r, 0.3)))

    def to_dict(self, ear_height: float = 1.6) -> dict:
        return {"id": self.id, "kind": self.kind, "r": round(self.r, 3), "az": round(self.az, 2),
                "el": round(self.el(ear_height), 1), "x": round(self.x, 3), "z": round(self.z, 3),
                "h_min": round(self.h_min, 2), "h_max": round(self.h_max, 2), "width": round(self.width, 2),
                "in_path": self.in_path, "suppressed": self.suppressed, "confirmed": self.confirmed,
                "vel_r": round(self.vel_r, 2), "first_seen": self.first_seen, "eligible_since": self.eligible_since,
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
        self.set_mount(cfg["sensor"]["mount_height"], cfg["sensor"]["mount_pitch_deg"])

    # ---------------------------------------------------------------- setup
    def set_mount(self, height: float, pitch_deg: float):
        self.mount_height = float(height)
        self.mount_pitch = float(pitch_deg)
        self.M = mount_matrix(self.mount_pitch)
        self.plane = None

    def reset(self):
        self.tracks = []
        self.plane = None

    def _prepare(self, intr: dict, shape):
        key = (intr["fx"], intr["fy"], intr["cx"], intr["cy"], shape, self.p["stride"])
        if key == self._intr_key:
            return
        s = int(self.p["stride"])
        H, W = shape
        v, u = np.mgrid[0:H:s, 0:W:s].astype(np.float32)
        self.xs = ((u - intr["cx"]) / intr["fx"]).ravel()
        self.ys = ((v - intr["cy"]) / intr["fy"]).ravel()
        self._intr_key = key

    # ---------------------------------------------------------------- frame
    def process(self, frame: DepthFrame, range_limit: float = 4.0, report_off_path: bool | None = None,
                ear_height: float = 1.6) -> ProcessResult:
        t0 = time.perf_counter()
        p = self.p
        s = int(p["stride"])
        depth = frame.depth
        self._prepare(frame.intrinsics, depth.shape)
        z = depth[::s, ::s].ravel()
        valid = z > 0
        valid_ratio = float(valid.mean()) if z.size else 0.0
        zc = z[valid]
        xc, yc = self.xs[valid] * zc, self.ys[valid] * zc
        M = self.M
        X = M[0, 0] * xc + M[0, 1] * yc + M[0, 2] * zc
        Y = M[1, 0] * xc + M[1, 1] * yc + M[1, 2] * zc + self.mount_height
        Z = M[2, 0] * xc + M[2, 1] * yc + M[2, 2] * zc
        t1 = time.perf_counter()

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
        found = []
        for part, in_path in ((obst & corr, True), (obst & ~corr, False)):
            found += self._clusters(X[part], Z[part], hgt[part], cell, gw, gd, drop=False, in_path=in_path)
        # for drop-off points, where the ray crossed the ground plane is the hole's lip
        if drop.any():
            sh = self._sensor_height()
            f = sh / (sh - hgt[drop])
            xd, zd = X[drop] * f, Z[drop] * f
            corr = np.abs(xd) <= p["corridor_half_width"]
            for part, in_path in ((corr, True), (~corr, False)):
                found += self._clusters(xd[part], zd[part], hgt[drop][part], cell, gw, gd, drop=True, in_path=in_path)
        t3 = time.perf_counter()

        self._track(found, frame.t_capture)
        if report_off_path is None:
            report_off_path = bool(p["report_off_path"])
        reported = self._prioritise(range_limit, report_off_path, frame.t_capture)
        t4 = time.perf_counter()

        ms = lambda a, b: round((b - a) * 1000, 2)  # noqa: E731
        return ProcessResult(
            seq=frame.seq, t_capture=frame.t_capture, tracks=list(self.tracks),
            reported=[t.to_dict(ear_height) for t in reported], valid_ratio=valid_ratio,
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
    def _clusters(self, X, Z, Hh, cell, gw, gd, drop: bool, in_path: bool) -> list[Obstacle]:
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
            elif h_min >= p["overhang_min_height"]:
                kind = "head"
            elif h_max <= p["low_max_height"]:
                kind = "low"
            else:
                kind = "body"
            suppressed = False
            if not drop and (z_max - z_min) > 1.2 and (x_max - x_min) < 0.4 and not (x_min <= body and x_max >= -body):
                kind, suppressed = "wall", True  # wall running parallel to the direction of travel
            out.append(Obstacle(kind, r_near, math.degrees(math.atan2(xn, zn)), xn, zn, float(h_min), float(h_max),
                                float(x_min), float(x_max), float(z_min), float(z_max), int(sel.size), in_path,
                                suppressed))
        return out

    # -------------------------------------------------------------- tracker
    def _track(self, found: list[Obstacle], t: float):
        p = self.p
        gate, alpha = p["assoc_gate"], p["smoothing"]
        pairs = []
        for i, tr in enumerate(self.tracks):
            for j, ob in enumerate(found):
                if (tr.kind == "drop") != (ob.kind == "drop"):
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
            new_r = (1 - alpha) * tr.r + alpha * ob.r
            tr.vel_r = 0.7 * tr.vel_r + 0.3 * ((ob.r - tr.r) / dt)
            # big jumps (object suddenly closer) are taken immediately: safety first
            tr.r = ob.r if ob.r < tr.r - 0.3 else new_r
            tr.az = (1 - alpha) * tr.az + alpha * ob.az
            tr.x, tr.z = ob.x, ob.z
            tr.h_min, tr.h_max = ob.h_min, ob.h_max
            tr.width = ob.x_max - ob.x_min
            tr.x_min, tr.x_max, tr.z_min, tr.z_max = ob.x_min, ob.x_max, ob.z_min, ob.z_max
            tr.in_path, tr.suppressed = ob.in_path, ob.suppressed
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
            if p["confirm_frames"] <= 1:
                tr.confirmed, tr.confirmed_at = True, t
            self._next_id += 1
            self.tracks.append(tr)
        self.tracks = [tr for tr in self.tracks if t - tr.last_seen <= p["drop_time"]]

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
            eff = tr.r - (bonus if tr.kind in ("head", "drop") else 0.0)
            return (0 if tr.in_path else 1, eff)

        return sorted(cands, key=key)
