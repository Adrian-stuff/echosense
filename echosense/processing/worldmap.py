"""World map (prototype): remember the surroundings while walking.

Each frame the pipeline sees only what is in front of the sensor right now. This module keeps a
**rolling top-down map** of the area around the user, in a fixed "world" frame, so that what
was seen a few seconds ago (a pole just passed, a curb that dropped out of view below) is still
known. It does *not* change the audio cues yet.

To put each frame into a fixed frame the map needs to know how the user moved between frames:
turning, sidestepping, walking forward. The Kinect v2 has no motion sensor and the Kinect v1's
accelerometer only gives "up", so the motion comes from the depth itself (**scan matching**):

1. level the frame with the fitted floor plane (so chest bob and tilt don't move the map),
2. keep the "structure" points (walls, poles, cars, people: 0.25 to 2 m above the floor) and thin
   them to one point per 5 cm cell,
3. find the planar pose (x, z, yaw) at which those points best line up with the map: Gauss-Newton
   on a blurred occupancy grid (as in Hector SLAM), coarse 10 cm grid first, then 5 cm. It starts
   from a walking motion model (last forward speed and turn rate), and a pull towards that guess
   keeps the pose from sliding where the view gives no constraint (a long blank wall constrains
   only one direction; an open plaza constrains nothing: the status then reads "weak" or "lost" and
   the pose is dead reckoning). The pull is much stronger sideways than forward or in heading:
   with a 70 deg field of view a small turn and a small sidestep look almost the same, and people
   walking turn far more often than they sidestep,
4. add the frame to the map. Cells fade out over time (``fade_s``), and faster where the floor is
   seen through them, so people who walked away and other moving things don't leave ghosts.

World frame = the pose at the first frame after a reset: x right, z forward, yaw positive
turning right (the simulator's convention, so its ground truth can be compared directly).
"""
from __future__ import annotations

import base64
import math

import numpy as np

DEFAULTS = {
    "enabled": True,
    "cell": 0.05,  # map resolution (m)
    "size": 16.0,  # rolling map is size x size metres, re-centred as the user walks
    "recenter": 3.0,  # re-centre once the user is this far from the middle (m)
    "h_min": 0.25,  # structure: points this high above the floor ...
    "h_max": 2.0,  # ... up to this
    "r_min": 0.4,
    "r_max": 4.5,  # depth noise grows with range: farther points are not used
    "edge_range": 0.5,  # scan points used for matching stay this far inside the range limit ...
    "edge_deg": 8.0,  # ... and this far inside the field of view's side edges
    "max_points": 600,  # scan points used for matching (after 5 cm thinning)
    "min_points": 15,  # fewer than this: no match, dead reckoning
    "search_deg": 30.0,  # after dead reckoning, look this far either side of the guessed heading ...
    "search_m": 0.3,  # ... and this far around the guessed position before refining
    "iterations": 6,  # Gauss-Newton steps per resolution level
    "field_radius": 4,  # distance field reaches this many cells (10 cm coarse grid, then 5 cm)
    "prior_side": 0.1,  # pull towards the motion model's guess, per point: sideways (1/m^2) ...
    "prior_forward": 0.005,  # ... forward (1/m^2)
    "prior_yaw": 0.005,  # ... and heading (1/rad^2)
    "max_jump": 0.1,  # a match further than this (m) from the guess is rejected
    "max_jump_deg": 8.0,
    "velocity_gain": 0.3,  # smoothing of the motion estimate used for the next frame's guess
    "velocity_gain_weak": 0.08,  # ... for walking speed while the match is weak
    "walk_limits": [0.6, -0.4, 2.0, 3.0],  # sideways |m/s|, forward min and max (m/s), turn |rad/s|
    "hit": 0.34,  # evidence added per frame a cell is seen occupied
    "free": 0.15,  # removed per frame the floor is seen in it
    "occupied": 0.5,  # evidence at which a cell counts as occupied
    "fade_s": 30.0,  # evidence time constant for cells not being looked at
    "good_score": 0.4,  # share of scan points on a mapped surface needed for "mapping" (else "weak")
    "good_constraint": 0.05,  # weakest translation constraint (per point) needed for "mapping"
    "trail_points": 600,
}

CAT_LOW, CAT_BODY, CAT_DROP = 1, 2, 3


def level_rotation(n: np.ndarray) -> np.ndarray:
    """Smallest rotation taking the floor normal n (body frame) to straight up (0, 1, 0)."""
    n = np.asarray(n, np.float64)
    n = n / np.linalg.norm(n)
    up = np.array([0.0, 1.0, 0.0])
    v = np.cross(n, up)
    s, c = float(np.linalg.norm(v)), float(n @ up)
    if s < 1e-9:
        return np.eye(3)
    k = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + k + k @ k * ((1 - c) / (s * s))


def likelihood_field(occ: np.ndarray, sigma_cells: float, radius: int = 3) -> np.ndarray:
    """max over nearby cells of occupancy x exp(-d^2 / 2 sigma^2): ~1 on a surface, falling off smoothly
    with the distance to it. A plain blur of a one-cell-wide wall is peaky (bilinear sampling gives
    only ~0.4 on the wall itself), which makes Gauss-Newton slide off the right pose. The Gaussian
    factorises, and so does the max of non-negative products, so this is two 1-D passes."""
    w = np.exp(-(np.arange(-radius, radius + 1) ** 2) / (2.0 * sigma_cells ** 2)).astype(np.float32)
    out = occ.copy()
    for axis in (0, 1):
        src = out
        out = src * w[radius]
        n = src.shape[axis]
        for k in range(1, radius + 1):
            if k >= n:
                break
            if axis == 0:
                np.maximum(out[k:], src[:-k] * w[radius + k], out=out[k:])
                np.maximum(out[:-k], src[k:] * w[radius + k], out=out[:-k])
            else:
                np.maximum(out[:, k:], src[:, :-k] * w[radius + k], out=out[:, k:])
                np.maximum(out[:, :-k], src[:, k:] * w[radius + k], out=out[:, :-k])
    return out


def distance_field(occupied: np.ndarray, cell: float, radius: int) -> np.ndarray:
    """Distance (m) from each cell centre to the nearest occupied cell centre, capped at radius cells.
    Matching minimises these distances (as ICP does point to surface): unlike a peak-shaped
    likelihood, its slope doesn't vanish at the surface, so Gauss-Newton isn't biased towards the
    starting guess."""
    sig = float(radius)
    L = likelihood_field(occupied.astype(np.float32), sig, radius)
    with np.errstate(divide="ignore"):
        d = sig * np.sqrt(np.maximum(-2.0 * np.log(L), 0.0))
    return (np.minimum(d, radius) * cell).astype(np.float32)


def _sample(L: np.ndarray, x0: float, z0: float, cell: float, wx: np.ndarray, wz: np.ndarray):
    """Bilinear value and gradient (per metre) of grid L (rows = z, cols = x) at world points.
    Points outside the grid get value 0 and gradient 0."""
    u = (wx - x0) / cell - 0.5
    v = (wz - z0) / cell - 0.5
    iu = np.floor(u).astype(np.int32)
    iv = np.floor(v).astype(np.int32)
    h, w = L.shape
    ok = (iu >= 0) & (iv >= 0) & (iu < w - 1) & (iv < h - 1)
    iu, iv = np.where(ok, iu, 0), np.where(ok, iv, 0)
    fu, fv = u - iu, v - iv
    m00, m01 = L[iv, iu], L[iv, iu + 1]
    m10, m11 = L[iv + 1, iu], L[iv + 1, iu + 1]
    val = (1 - fv) * ((1 - fu) * m00 + fu * m01) + fv * ((1 - fu) * m10 + fu * m11)
    gx = ((1 - fv) * (m01 - m00) + fv * (m11 - m10)) / cell
    gz = ((1 - fu) * (m10 - m00) + fu * (m11 - m01)) / cell
    return np.where(ok, val, 0.0), np.where(ok, gx, 0.0), np.where(ok, gz, 0.0), ok


def _wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


class WorldMapper:
    def __init__(self, cfg: dict | None = None):
        self.c = {**DEFAULTS, **(cfg or {})}
        self.cell = float(self.c["cell"])
        self.n = int(round(float(self.c["size"]) / self.cell))
        self.rng = np.random.default_rng(3)
        self.reset()

    # ---------------------------------------------------------------- state
    def reset(self):
        n = self.n
        self.E = np.zeros((n, n), np.float32)  # structure evidence, rows = z, cols = x
        self.D = np.zeros((n, n), np.float32)  # drop-off evidence
        self.Hm = np.zeros((n, n), np.float32)  # tallest point seen in the cell (m above floor)
        half = n * self.cell / 2
        self.x0 = self.z0 = -half  # world position of the grid's corner
        self.pose = np.zeros(3)  # x, z, yaw (rad)
        self.vel = np.zeros(3)  # body frame, per second: sideways (m), forward (m), turn (rad)
        self.t_last = None
        self.frames = 0
        self.status = "starting"
        self.score = 0.0
        self.constraint = 0.0
        self.n_scan = 0
        self.lost_for = 0.0
        self.travelled = 0.0
        self.trail: list = []
        self.truth0 = None
        self.truth_trail: list = []
        self.err = None  # vs. ground truth (simulator only)
        self.version = 0

    # ---------------------------------------------------------------- frame
    def update(self, X, Y, Z, hgt, normal, sensor_height: float, t: float, truth: dict | None = None):
        """Add one frame. X, Y, Z: body-frame points (sensor at the origin of X/Z); hgt: height above the
        fitted floor; normal: the floor's normal in the body frame. ``truth``: the simulator's sensor pose,
        for evaluation only (never used for the estimate)."""
        c = self.c
        R = level_rotation(normal)
        xl = R[0, 0] * X + R[0, 1] * Y + R[0, 2] * Z
        zl = R[2, 0] * X + R[2, 1] * Y + R[2, 2] * Z
        rh = np.hypot(xl, zl)
        near = (rh > c["r_min"]) & (rh < c["r_max"])
        st = near & (hgt > c["h_min"]) & (hgt < c["h_max"])
        px, pz, ph = self._thin(xl[st], zl[st], hgt[st])
        # match only points well inside what the sensor sees: just past the edges of the last view
        # (range limit ahead, field-of-view edges when turning) the map has nothing yet, so those points
        # would snap to where the mapped wall *seemed* to end and drag the pose back
        az = np.arctan2(xl[near], zl[near])
        inner = np.hypot(px, pz) < c["r_max"] - c["edge_range"]
        if az.size > 50:
            lo, hi = np.percentile(az, [0.5, 99.5])
            paz = np.arctan2(px, pz)
            inner &= (paz > lo + math.radians(c["edge_deg"])) & (paz < hi - math.radians(c["edge_deg"]))
        mx, mz = px[inner], pz[inner]
        self.n_scan = mx.size

        dt = 0.0 if self.t_last is None else min(max(t - self.t_last, 0.0), 0.25)
        self.t_last = t
        pred = self._predict(dt)
        new, matched, good = pred, False, False
        if self.frames > 0 and mx.size >= c["min_points"]:
            new, matched, good = self._match(mx, mz, pred)
        else:
            self.score, self.constraint = 0.0, 0.0
        if self.frames == 0:
            self.status = "starting"
        else:
            self.status = "mapping" if good else "weak" if matched else "lost"
        self.lost_for = 0.0 if matched else self.lost_for + dt
        if dt > 0:
            d = new - self.pose
            d[2] = _wrap(d[2])
            if matched:
                hm = self.pose[2] + d[2] / 2  # heading half way through the step
                cs, sn = math.cos(hm), math.sin(hm)
                v = np.array([cs * d[0] - sn * d[1], sn * d[0] + cs * d[1], d[2]]) / dt  # sideways, forward, turn
                a = np.full(3, float(c["velocity_gain"]))
                if not good:  # the view barely constrains the position: trust the walking speed more than this step
                    a[:2] = float(c["velocity_gain_weak"])
                self.vel = (1 - a) * self.vel + a * v
                lim = c["walk_limits"]  # people walking: no fast reversing or sliding sideways
                self.vel = np.clip(self.vel, [-lim[0], lim[1], -lim[3]], [lim[0], lim[2], lim[3]])
            else:  # dead reckoning: a turn doesn't go on by itself; walking does, for a while
                self.vel[0] *= 0.8
                self.vel[2] *= 0.8
                if self.lost_for > 1.5:
                    self.vel[1] *= 0.95
            self.travelled += float(np.hypot(d[0], d[1]))
        self.pose = np.array([new[0], new[1], _wrap(new[2])])
        self._recenter()

        # floor seen through a cell clears it (things that moved away); drop lips where the ray crossed the floor
        fl = near & (np.abs(hgt) < 0.05)
        fidx = np.flatnonzero(fl)
        if fidx.size > 4000:
            fidx = fidx[:: fidx.size // 4000 + 1]
        dr = near & (hgt < -0.12) & (hgt > -1.5)
        f = sensor_height / np.maximum(sensor_height - hgt[dr], 1e-3)
        self._insert(px, pz, ph, xl[fidx], zl[fidx], xl[dr] * f, zl[dr] * f, dt)
        self.frames += 1
        self._log_trail(truth)

    def _predict(self, dt: float) -> np.ndarray:
        """Walking motion model: the last forward speed, sideways speed and turn rate (body frame) go on."""
        vl, vf, w = self.vel
        hm = self.pose[2] + w * dt / 2
        cs, sn = math.cos(hm), math.sin(hm)
        return np.array([self.pose[0] + (cs * vl + sn * vf) * dt, self.pose[1] + (-sn * vl + cs * vf) * dt,
                         _wrap(self.pose[2] + w * dt)])

    def _prior(self, pred, N: int) -> np.ndarray:
        """How strongly the match is held to the motion model's guess, per scan point. Sideways steps are
        rare when walking, while a narrow field of view can hardly tell a small turn from a small sidestep,
        so sideways (in the predicted body frame) is held much harder than forward motion and turning."""
        c = self.c
        cs, sn = math.cos(pred[2]), math.sin(pred[2])
        side, fwd = np.array([cs, -sn]), np.array([sn, cs])
        lam = np.zeros((3, 3))
        lam[:2, :2] = c["prior_side"] * np.outer(side, side) + c["prior_forward"] * np.outer(fwd, fwd)
        lam[2, 2] = c["prior_yaw"]
        return lam * N

    def _thin(self, x, z, h):
        """One point per 5 cm cell (the mean), at most max_points of them; also the cell's tallest point."""
        if x.size == 0:
            return x, z, h
        k = np.floor(x / 0.05).astype(np.int64) * 100003 + np.floor(z / 0.05).astype(np.int64)
        u, inv, cnt = np.unique(k, return_inverse=True, return_counts=True)
        sx = np.bincount(inv, x, u.size) / cnt
        sz = np.bincount(inv, z, u.size) / cnt
        sh = np.zeros(u.size)
        np.maximum.at(sh, inv, h)
        if u.size > self.c["max_points"]:
            pick = self.rng.choice(u.size, int(self.c["max_points"]), replace=False)
            sx, sz, sh = sx[pick], sz[pick], sh[pick]
        return sx, sz, sh

    # -------------------------------------------------------------- matching
    def _window(self, pose, half_m: float):
        """Crop of the evidence grid around a pose: (occupancy 0..1, x0, z0)."""
        n, cell = self.n, self.cell
        ci = int((pose[0] - self.x0) / cell)
        cj = int((pose[1] - self.z0) / cell)
        r = int(half_m / cell)
        i0, i1 = max(0, ci - r), min(n, ci + r)
        j0, j1 = max(0, cj - r), min(n, cj + r)
        if i1 - i0 < 4 or j1 - j0 < 4:
            return None
        occ = np.minimum(self.E[j0:j1, i0:i1] * (1.0 / self.c["occupied"]), 1.0)
        return occ, self.x0 + i0 * cell, self.z0 + j0 * cell

    def _match(self, px, pz, pred):
        c = self.c
        win = self._window(pred, c["r_max"] + 1.0)
        if win is None:
            return pred, False, False
        occ, wx0, wz0 = win
        if int((occ > 0.5).sum()) < c["min_points"]:
            return pred, False, False
        occ = occ >= 1.0
        h, w = occ.shape
        coarse = occ[: h // 2 * 2, : w // 2 * 2].reshape(h // 2, 2, w // 2, 2).max(axis=(1, 3))
        rad = int(c["field_radius"])
        levels = [(distance_field(coarse, self.cell * 2, rad), self.cell * 2), (distance_field(occ, self.cell, rad), self.cell)]
        N = px.size
        lam = self._prior(pred, N)
        pose = pred.copy()
        recovering = self.lost_for > 0 or self.status == "lost"
        if recovering:  # dead reckoning drifted: find the heading and position again before refining
            pose = self._search(px, pz, pred, levels[0][0], wx0, wz0, levels[0][1], rad)
        A = np.zeros((3, 3))
        dist = np.full(N, np.inf)
        for F, cell in levels:
            cap = rad * cell * 0.999
            for _ in range(int(c["iterations"])):
                cs, sn = math.cos(pose[2]), math.sin(pose[2])
                wx = pose[0] + cs * px + sn * pz
                wz = pose[1] - sn * px + cs * pz
                dist, gx, gz, inside = _sample(F, wx0, wz0, cell, wx, wz)
                use = inside & (dist < cap)  # points with no surface nearby (new view, moving things) don't pull
                gx, gz, r = gx[use], gz[use], dist[use]
                dth = np.stack([-sn * px[use] + cs * pz[use], -cs * px[use] - sn * pz[use]])
                J = np.stack([gx, gz, gx * dth[0] + gz * dth[1]], 1)
                A = J.T @ J
                g = -(J.T @ r)
                diff = pose - pred
                diff[2] = _wrap(diff[2])
                try:
                    step = np.linalg.solve(A + lam, g - lam @ diff)
                except np.linalg.LinAlgError:
                    break
                step[:2] = np.clip(step[:2], -cell, cell)  # stay inside the basin of the current level
                step[2] = float(np.clip(step[2], -0.05, 0.05))
                pose = pose + step
                if abs(step[0]) + abs(step[1]) < 2e-4 and abs(step[2]) < 2e-4:
                    break
            if not np.isfinite(dist).all():
                dist = np.where(np.isfinite(dist), dist, cap)
        self.score = float((dist < 0.06).mean())  # share of scan points within 6 cm of a mapped surface
        self.constraint = float(np.linalg.eigvalsh(A[:2, :2])[0] / N)
        jump = pose - pred
        lim_m, lim_deg = c["max_jump"], c["max_jump_deg"]
        if recovering:
            lim_m, lim_deg = lim_m + c["search_m"], lim_deg + c["search_deg"]
        if math.hypot(jump[0], jump[1]) > lim_m or abs(_wrap(jump[2])) > math.radians(lim_deg):
            return pred, False, False
        good = self.score >= c["good_score"] and self.constraint >= c["good_constraint"]
        return pose, True, good

    def _search(self, px, pz, pred, F, x0, z0, cell, rad):
        """Brute-force search on the coarse distance field: headings within search_deg and positions within
        search_m of the guess. Returns the best pose (the guess itself unless something is clearly better)."""
        c = self.c
        cap = rad * cell
        yaws = pred[2] + np.radians(np.arange(-c["search_deg"], c["search_deg"] + 1e-6, 2.0))
        offs = np.arange(-c["search_m"], c["search_m"] + 1e-6, 0.1)
        ox, oz = np.meshgrid(offs, offs)
        ox, oz = ox.ravel(), oz.ravel()
        best, best_cost = pred.copy(), None
        for yw in yaws:
            cs, sn = math.cos(yw), math.sin(yw)
            bx = cs * px + sn * pz  # rotated scan, before translation
            bz = -sn * px + cs * pz
            wx = pred[0] + ox[:, None] + bx[None, :]
            wz = pred[1] + oz[:, None] + bz[None, :]
            d = _sample(F, x0, z0, cell, wx.ravel(), wz.ravel())[0].reshape(wx.shape)
            cost = np.minimum(d, cap).mean(axis=1)
            k = int(np.argmin(cost))
            if best_cost is None or cost[k] < best_cost:
                best_cost, best = float(cost[k]), np.array([pred[0] + ox[k], pred[1] + oz[k], yw])
        return best

    # -------------------------------------------------------------- the map
    def _cells(self, wx, wz):
        i = np.floor((wx - self.x0) / self.cell).astype(np.int64)
        j = np.floor((wz - self.z0) / self.cell).astype(np.int64)
        ok = (i >= 0) & (j >= 0) & (i < self.n) & (j < self.n)
        return j[ok] * self.n + i[ok], ok

    def _to_world(self, x, z):
        cs, sn = math.cos(self.pose[2]), math.sin(self.pose[2])
        return self.pose[0] + cs * x + sn * z, self.pose[1] - sn * x + cs * z

    def _insert(self, px, pz, ph, fx, fz, dx, dz, dt):
        c = self.c
        if dt > 0:
            k = math.exp(-dt / float(c["fade_s"]))
            self.E *= k
            self.D *= k
        E, D, Hm = self.E.ravel(), self.D.ravel(), self.Hm.ravel()
        hit = np.zeros(0, np.int64)
        if px.size:
            hit, ok = self._cells(*self._to_world(px, pz))
            hu = np.unique(hit)
            E[hu] = np.minimum(E[hu] + c["hit"], 1.0)
            hmax = np.zeros(E.size, np.float32)  # tallest point per cell this frame (sparse use)
            np.maximum.at(hmax, hit, ph[ok].astype(np.float32))
            old = Hm[hu]
            Hm[hu] = np.where(old > 0, 0.6 * old + 0.4 * hmax[hu], hmax[hu])
        if fx.size:
            fc, _ = self._cells(*self._to_world(fx, fz))
            fc = np.setdiff1d(np.unique(fc), hit, assume_unique=False)
            E[fc] = np.maximum(E[fc] - c["free"], 0.0)
            D[fc] = np.maximum(D[fc] - c["free"] * 0.5, 0.0)
        if dx.size:
            dc, _ = self._cells(*self._to_world(dx, dz))
            du = np.unique(dc)
            D[du] = np.minimum(D[du] + c["hit"], 1.0)
        self.version += 1

    def _recenter(self):
        half = self.n * self.cell / 2
        cx, cz = self.x0 + half, self.z0 + half
        if max(abs(self.pose[0] - cx), abs(self.pose[1] - cz)) < self.c["recenter"]:
            return
        si = int(round((self.pose[0] - cx) / self.cell))
        sj = int(round((self.pose[1] - cz) / self.cell))
        for name in ("E", "D", "Hm"):
            a = getattr(self, name)
            b = np.zeros_like(a)
            n = self.n
            src_i, dst_i = slice(max(si, 0), n + min(si, 0)), slice(max(-si, 0), n + min(-si, 0))
            src_j, dst_j = slice(max(sj, 0), n + min(sj, 0)), slice(max(-sj, 0), n + min(-sj, 0))
            b[dst_j, dst_i] = a[src_j, src_i]
            setattr(self, name, b)
        self.x0 += si * self.cell
        self.z0 += sj * self.cell

    # ------------------------------------------------------------ reporting
    def _log_trail(self, truth):
        p = (round(float(self.pose[0]), 3), round(float(self.pose[1]), 3))
        if not self.trail or math.hypot(p[0] - self.trail[-1][0], p[1] - self.trail[-1][1]) > 0.08:
            self.trail.append(p)
            del self.trail[: max(0, len(self.trail) - int(self.c["trail_points"]))]
        if truth is None:
            return
        tx, tz, ty = float(truth["x"]), float(truth["z"]), math.radians(float(truth["yaw"]))
        if self.truth0 is None:
            self.truth0 = (tx, tz, ty)
        x0, z0, y0 = self.truth0
        cs, sn = math.cos(y0), math.sin(y0)
        dx, dz = tx - x0, tz - z0
        gt = (cs * dx - sn * dz, sn * dx + cs * dz, _wrap(ty - y0))  # truth in the map's frame
        q = (round(gt[0], 3), round(gt[1], 3))
        if not self.truth_trail or math.hypot(q[0] - self.truth_trail[-1][0], q[1] - self.truth_trail[-1][1]) > 0.08:
            self.truth_trail.append(q)
            del self.truth_trail[: max(0, len(self.truth_trail) - int(self.c["trail_points"]))]
        self.err = {"pos": float(math.hypot(self.pose[0] - gt[0], self.pose[1] - gt[1])),
                    "yaw_deg": float(math.degrees(abs(_wrap(self.pose[2] - gt[2]))))}

    def state(self) -> dict:
        out = {"status": self.status, "x": round(float(self.pose[0]), 3), "z": round(float(self.pose[1]), 3),
               "yaw": round(math.degrees(self.pose[2]), 2), "score": round(self.score, 3),
               "constraint": round(self.constraint, 2), "points": self.n_scan, "travelled": round(self.travelled, 2)}
        if self.err is not None:
            out["err_pos"] = round(self.err["pos"], 3)
            out["err_yaw"] = round(self.err["yaw_deg"], 2)
        return out

    def snapshot(self, view_m: float = 10.0, step: int = 2) -> dict:
        """The map around the user for the console: view_m x view_m metres at step x cell resolution,
        one byte per cell = category (1 low, 2 body height, 3 drop-off) << 6 | confidence (0..63)."""
        cell = self.cell * step
        r = int(view_m / 2 / cell)
        ci = int((self.pose[0] - self.x0) / cell)
        cj = int((self.pose[1] - self.z0) / cell)
        m = self.n // step
        E = self.E[: m * step, : m * step].reshape(m, step, m, step).max(axis=(1, 3))
        D = self.D[: m * step, : m * step].reshape(m, step, m, step).max(axis=(1, 3))
        H = self.Hm[: m * step, : m * step].reshape(m, step, m, step).max(axis=(1, 3))
        i0, j0 = ci - r, cj - r
        out = np.zeros((2 * r, 2 * r), np.uint8)
        si, sj = slice(max(i0, 0), min(i0 + 2 * r, m)), slice(max(j0, 0), min(j0 + 2 * r, m))
        di = slice(si.start - i0, si.stop - i0)
        dj = slice(sj.start - j0, sj.stop - j0)
        e, d, h = E[sj, si], D[sj, si], H[sj, si]
        occ = float(self.c["occupied"])
        cat = np.where(d >= occ, CAT_DROP, np.where(e >= occ, np.where(h < 0.45, CAT_LOW, CAT_BODY), 0))
        conf = np.clip(np.where(cat == CAT_DROP, d, e) * 63, 0, 63).astype(np.uint8)
        out[dj, di] = np.where(cat > 0, (cat.astype(np.uint8) << 6) | conf, 0)
        return {"cell": round(cell, 4), "w": 2 * r, "h": 2 * r, "x0": round(self.x0 + i0 * cell, 3),
                "z0": round(self.z0 + j0 * cell, 3), "data": base64.b64encode(out.tobytes()).decode(),
                "trail": self.trail[-300:], "truth": self.truth_trail[-300:] or None, **self.state()}
