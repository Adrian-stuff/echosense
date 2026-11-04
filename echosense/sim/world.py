"""Virtual 3D world and a Kinect v2 depth-camera model (numpy ray casting).

Coordinate frames
-----------------
World:  X right/east, Y up, Z forward/north, metres, ground plane at Y = 0.
Camera: x right, y down, z forward (the usual depth-camera convention).

Rays are generated with a camera-space z component of exactly 1, so the ray
parameter ``t`` of a hit equals the *z-depth* that a Kinect reports (not the
radial distance). The noise model follows published Kinect v2 behaviour:
roughly 1.5 mm axial noise up close growing ~quadratically with range,
invalid (0) readings outside 0.5-4.5 m, material-dependent dropouts, glass
that the IR mostly passes through, flying pixels on depth edges, and extra
dropouts under sunlight.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from ..config import SCENES_DIR

# name: axial-noise multiplier, dropout probability, pass-through probability
MATERIALS = {
    "asphalt": (1.2, 0.004, 0.0),
    "concrete": (1.0, 0.002, 0.0),
    "metal": (1.6, 0.04, 0.0),
    "glass": (2.5, 0.20, 0.70),
    "fabric": (1.3, 0.06, 0.0),
    "foliage": (3.0, 0.10, 0.35),
    "skin": (1.2, 0.01, 0.0),
    "wood": (1.1, 0.005, 0.0),
}
MAT_NAMES = list(MATERIALS)
MAT_INDEX = {n: i for i, n in enumerate(MAT_NAMES)}
MAT_NOISE = np.array([MATERIALS[n][0] for n in MAT_NAMES], np.float32)
MAT_DROPOUT = np.array([MATERIALS[n][1] for n in MAT_NAMES], np.float32)
MAT_PASS = np.array([MATERIALS[n][2] for n in MAT_NAMES], np.float32)

INF = np.float32(np.inf)


def rot_y(yaw: float) -> np.ndarray:
    """Rotation about +Y. Positive yaw turns the forward axis (+Z) towards +X."""
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]], np.float32)


def rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]], np.float32)


# camera (x right, y down, z fwd) -> body/world-aligned (x right, y up, z fwd)
CAM_TO_BODY = np.array([[1, 0, 0], [0, -1, 0], [0, 0, 1]], np.float32)


def camera_rotation(yaw: float, pitch: float) -> np.ndarray:
    """Camera->world rotation for a sensor with heading ``yaw`` and ``pitch``
    (radians, positive pitch = tilted up)."""
    return rot_y(yaw) @ rot_x(-pitch) @ CAM_TO_BODY


class Primitive:
    kind = "primitive"

    def __init__(self, name: str, material: str):
        self.name = name
        self.material = material if material in MAT_INDEX else "concrete"
        self.mat_id = MAT_INDEX[self.material]

    def aabb(self):
        raise NotImplementedError

    def intersect(self, o: np.ndarray, d: np.ndarray) -> np.ndarray:
        raise NotImplementedError

    def collides(self, x: float, z: float, radius: float, height: float) -> bool:
        raise NotImplementedError

    def to_dict(self) -> dict:
        raise NotImplementedError


class Box(Primitive):
    kind = "box"

    def __init__(self, name, x, z, w, h, d, y0=0.0, yaw=0.0, material="concrete"):
        super().__init__(name, material)
        self.w, self.h, self.d, self.y0 = float(w), float(h), float(d), float(y0)
        self.yaw_deg = float(yaw)
        self.set_position(x, z)
        self._R = rot_y(math.radians(self.yaw_deg))
        self.he = np.array([w / 2, h / 2, d / 2], np.float32)

    def set_position(self, x, z):
        self.x, self.z = float(x), float(z)
        self.c = np.array([self.x, self.y0 + self.h / 2, self.z], np.float32)

    def aabb(self):
        hx, hz = self.w / 2, self.d / 2
        a = math.radians(self.yaw_deg)
        ex = abs(math.cos(a)) * hx + abs(math.sin(a)) * hz
        ez = abs(math.sin(a)) * hx + abs(math.cos(a)) * hz
        return (np.array([self.x - ex, self.y0, self.z - ez]), np.array([self.x + ex, self.y0 + self.h, self.z + ez]))

    def intersect(self, o, d):
        if self.yaw_deg:
            ol = (o - self.c) @ self._R  # == R^T (o - c)
            dl = d @ self._R
        else:
            ol, dl = o - self.c, d
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / dl
            t1 = (-self.he - ol) * inv
            t2 = (self.he - ol) * inv
        tmin = np.minimum(t1, t2).max(axis=-1)
        tmax = np.maximum(t1, t2).min(axis=-1)
        hit = (tmax >= tmin) & (tmin > 1e-3)
        return np.where(hit, tmin, INF).astype(np.float32)

    def collides(self, x, z, radius, height):
        if self.y0 > height:
            return False
        p = np.array([x - self.x, 0.0, z - self.z], np.float32) @ self._R
        qx = min(max(p[0], -self.w / 2), self.w / 2)
        qz = min(max(p[2], -self.d / 2), self.d / 2)
        return (p[0] - qx) ** 2 + (p[2] - qz) ** 2 < radius * radius

    def to_dict(self):
        return dict(type="box", name=self.name, x=self.x, z=self.z, w=self.w, h=self.h, d=self.d,
                    y0=self.y0, yaw=self.yaw_deg, material=self.material)


class Cylinder(Primitive):
    """Vertical cylinder: poles, posts, tree trunks, bollards, bodies."""

    kind = "cylinder"

    def __init__(self, name, x, z, r, y1, y0=0.0, material="concrete"):
        super().__init__(name, material)
        self.r, self.y0, self.y1 = float(r), float(y0), float(y1)
        self.set_position(x, z)

    def set_position(self, x, z):
        self.x, self.z = float(x), float(z)

    def aabb(self):
        return (np.array([self.x - self.r, self.y0, self.z - self.r]), np.array([self.x + self.r, self.y1, self.z + self.r]))

    def intersect(self, o, d):
        ox, oy, oz = o[0] - self.x, o[1], o[2] - self.z
        dx, dy, dz = d[..., 0], d[..., 1], d[..., 2]
        a = dx * dx + dz * dz
        b = 2.0 * (ox * dx + oz * dz)
        c = ox * ox + oz * oz - self.r * self.r
        disc = b * b - 4.0 * a * c
        with np.errstate(invalid="ignore", divide="ignore"):
            t_side = (-b - np.sqrt(disc)) / (2.0 * a)
            y = oy + t_side * dy
            side_ok = (disc >= 0) & (t_side > 1e-3) & (y >= self.y0) & (y <= self.y1)
            t_cap = (self.y1 - oy) / dy
            cx = ox + t_cap * dx
            cz = oz + t_cap * dz
            cap_ok = (t_cap > 1e-3) & (cx * cx + cz * cz <= self.r * self.r)
        t = np.where(side_ok, t_side, INF)
        t = np.where(cap_ok & (t_cap < t), t_cap, t)
        return t.astype(np.float32)

    def collides(self, x, z, radius, height):
        if self.y0 > height:
            return False
        return (x - self.x) ** 2 + (z - self.z) ** 2 < (radius + self.r) ** 2

    def to_dict(self):
        return dict(type="cylinder", name=self.name, x=self.x, z=self.z, r=self.r, y0=self.y0, y1=self.y1,
                    material=self.material)


class Sphere(Primitive):
    kind = "sphere"

    def __init__(self, name, x, y, z, r, material="foliage"):
        super().__init__(name, material)
        self.y, self.r = float(y), float(r)
        self.set_position(x, z)

    def set_position(self, x, z):
        self.x, self.z = float(x), float(z)

    def aabb(self):
        r = self.r
        return (np.array([self.x - r, self.y - r, self.z - r]), np.array([self.x + r, self.y + r, self.z + r]))

    def intersect(self, o, d):
        oc = o - np.array([self.x, self.y, self.z], np.float32)
        a = (d * d).sum(-1)
        b = 2.0 * (d @ oc)
        c = float(oc @ oc) - self.r * self.r
        disc = b * b - 4 * a * c
        with np.errstate(invalid="ignore"):
            t = (-b - np.sqrt(disc)) / (2 * a)
        return np.where((disc >= 0) & (t > 1e-3), t, INF).astype(np.float32)

    def collides(self, x, z, radius, height):
        if self.y - self.r > height:
            return False
        return (x - self.x) ** 2 + (z - self.z) ** 2 < (radius + self.r * 0.8) ** 2

    def to_dict(self):
        return dict(type="sphere", name=self.name, x=self.x, y=self.y, z=self.z, r=self.r, material=self.material)


class Pit:
    """Rectangular hole in the ground (open drainage, missing manhole cover, step down)."""

    def __init__(self, name, x0, x1, z0, z1, depth):
        self.name = name
        self.x0, self.x1, self.z0, self.z1, self.depth = map(float, (x0, x1, z0, z1, depth))
        self.lo = np.array([self.x0, -self.depth, self.z0], np.float32)
        self.hi = np.array([self.x1, 0.0, self.z1], np.float32)

    def aabb(self):
        return self.lo, self.hi

    def exit_t(self, o, d):
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / d
            t1 = (self.lo - o) * inv
            t2 = (self.hi - o) * inv
        return np.maximum(t1, t2).min(axis=-1)

    def collides(self, x, z, radius, height):
        return self.x0 <= x <= self.x1 and self.z0 <= z <= self.z1

    def to_dict(self):
        return dict(type="pit", name=self.name, x0=self.x0, x1=self.x1, z0=self.z0, z1=self.z1, depth=self.depth)


class Pedestrian:
    """A person walking back and forth along a straight path."""

    def __init__(self, name, path, speed=0.9, phase=0.0):
        self.name = name
        self.p0 = np.array(path[0], float)
        self.p1 = np.array(path[1], float)
        self.speed = float(speed)
        self.phase = float(phase)
        self.length = float(np.linalg.norm(self.p1 - self.p0)) or 1.0
        self.body = Cylinder(name, *self.p0, r=0.22, y1=1.45, material="fabric")
        self.head = Sphere(name, self.p0[0], 1.60, self.p0[1], r=0.12, material="skin")
        self.x, self.z = float(self.p0[0]), float(self.p0[1])

    def update(self, t: float):
        s = (t * self.speed + self.phase * self.length) % (2 * self.length)
        f = s / self.length if s <= self.length else 2 - s / self.length
        p = self.p0 + (self.p1 - self.p0) * f
        self.x, self.z = float(p[0]), float(p[1])
        self.body.set_position(self.x, self.z)
        self.head.set_position(self.x, self.z)

    @property
    def primitives(self):
        return (self.body, self.head)

    def to_dict(self):
        return dict(type="pedestrian", name=self.name, path=[self.p0.tolist(), self.p1.tolist()], speed=self.speed)


def list_scenes() -> list[dict]:
    out = []
    for p in sorted(SCENES_DIR.glob("*.json")):
        try:
            d = json.loads(p.read_text())
            out.append({"id": p.stem, "name": d.get("name", p.stem), "description": d.get("description", "")})
        except Exception:  # pragma: no cover - broken user scene file
            continue
    return out


def load_scene(name_or_path: str | dict) -> dict:
    if isinstance(name_or_path, dict):
        return name_or_path
    p = Path(name_or_path)
    if not p.exists():
        p = SCENES_DIR / f"{name_or_path}.json"
    data = json.loads(p.read_text())
    data.setdefault("id", p.stem)
    return data


class World:
    def __init__(self, scene: dict | str):
        self.scene = load_scene(scene)
        self.static: list[Primitive] = []
        self.pits: list[Pit] = []
        self.movers: list[Pedestrian] = []
        for o in self.scene.get("objects", []):
            o = dict(o)
            t = o.pop("type")
            name = o.pop("name", t)
            if t == "box":
                self.static.append(Box(name, **o))
            elif t == "cylinder":
                self.static.append(Cylinder(name, **o))
            elif t == "sphere":
                self.static.append(Sphere(name, **o))
            elif t == "pit":
                self.pits.append(Pit(name, **o))
            elif t == "pedestrian":
                self.movers.append(Pedestrian(name, **o))
            else:
                raise ValueError(f"unknown object type {t!r}")
        self.ground_material = MAT_INDEX.get(self.scene.get("ground", "asphalt"), 0)

    # ------------------------------------------------------------------ state
    @property
    def primitives(self):
        prims = list(self.static)
        for m in self.movers:
            prims.extend(m.primitives)
        return prims

    def update(self, t: float):
        for m in self.movers:
            m.update(t)

    def collisions(self, x: float, z: float, radius: float = 0.25, height: float = 1.75) -> list[str]:
        hits = [p.name for p in self.static if p.collides(x, z, radius, height)]
        hits += [m.name for m in self.movers if m.body.collides(x, z, radius, height)]
        hits += [p.name for p in self.pits if p.collides(x, z, radius, height)]
        return hits

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.scene.items() if k != "objects"}
        d["objects"] = [p.to_dict() for p in self.static] + [p.to_dict() for p in self.pits] + [m.to_dict() for m in self.movers]
        return d

    def mover_states(self) -> list[dict]:
        return [{"name": m.name, "x": round(m.x, 3), "z": round(m.z, 3)} for m in self.movers]

    # -------------------------------------------------------------- rendering
    def render(self, o: np.ndarray, R: np.ndarray, cam: "CameraModel", rng: np.random.Generator,
               sun: float = 0.0, blocked: bool = False) -> np.ndarray:
        H, W = cam.height, cam.width
        if blocked:
            # Hand / cloth over the lens: everything is closer than min depth.
            depth = np.zeros((H, W), np.float32)
            speck = rng.random((H, W), dtype=np.float32) < 0.01
            depth[speck] = rng.uniform(0.5, 0.6, int(speck.sum())).astype(np.float32)
            return depth

        o = np.asarray(o, np.float32)
        dirs = (cam.dirs.reshape(-1, 3) @ R.T).reshape(H, W, 3)
        dy = dirs[..., 1]
        with np.errstate(divide="ignore"):
            zbuf = np.where(dy < -1e-6, -o[1] / dy, INF).astype(np.float32)
        mat = np.full((H, W), self.ground_material, np.int8)
        max_r = cam.max_depth * 1.6

        for pit in self.pits:
            win = cam.window(*pit.aabb(), o, R)
            if win is None:
                continue
            v0, v1, u0, u1 = win
            sub, zv, mv = dirs[v0:v1, u0:u1], zbuf[v0:v1, u0:u1], mat[v0:v1, u0:u1]
            px = o[0] + zv * sub[..., 0]
            pz = o[2] + zv * sub[..., 2]
            inside = np.isfinite(zv) & (px > pit.x0) & (px < pit.x1) & (pz > pit.z0) & (pz < pit.z1)
            if inside.any():
                zv[inside] = pit.exit_t(o, sub[inside])
                mv[inside] = MAT_INDEX["concrete"]

        for prim in self.primitives:
            lo, hi = prim.aabb()
            near = np.clip(o, lo, hi)
            if math.hypot(near[0] - o[0], near[2] - o[2]) > max_r:
                continue
            win = cam.window(lo, hi, o, R)
            if win is None:
                continue
            v0, v1, u0, u1 = win
            sub = dirs[v0:v1, u0:u1]
            t = prim.intersect(o, sub)
            p_pass = MAT_PASS[prim.mat_id]
            if p_pass > 0:
                t[rng.random(t.shape, dtype=np.float32) < p_pass] = INF
            zv, mv = zbuf[v0:v1, u0:u1], mat[v0:v1, u0:u1]
            closer = t < zv
            zv[closer] = t[closer]
            mv[closer] = prim.mat_id

        return cam.apply_noise(zbuf, mat, rng, sun)


class CameraModel:
    """Pinhole model + Kinect v2 style sensor noise."""

    def __init__(self, width, height, fx, fy, cx, cy, min_depth=0.5, max_depth=4.5):
        self.width, self.height = int(width), int(height)
        self.fx, self.fy, self.cx, self.cy = float(fx), float(fy), float(cx), float(cy)
        self.min_depth, self.max_depth = float(min_depth), float(max_depth)
        u, v = np.meshgrid(np.arange(self.width, dtype=np.float32), np.arange(self.height, dtype=np.float32))
        self.dirs = np.stack([(u - self.cx) / self.fx, (v - self.cy) / self.fy, np.ones_like(u)], -1).astype(np.float32)

    @classmethod
    def from_config(cls, s: dict) -> "CameraModel":
        return cls(s["width"], s["height"], s["fx"], s["fy"], s["cx"], s["cy"], s["min_depth"], s["max_depth"])

    def window(self, lo, hi, o, R):
        """Image-space bounding rectangle (v0, v1, u0, u1) of a world AABB, or None."""
        xs = (lo[0], hi[0])
        ys = (lo[1], hi[1])
        zs = (lo[2], hi[2])
        corners = np.array([[x, y, z] for x in xs for y in ys for z in zs], np.float32)
        pc = (corners - o) @ R  # world -> camera
        z = pc[:, 2]
        if (z <= 0.05).all():
            return None
        if (z <= 0.05).any():
            return (0, self.height, 0, self.width)
        u = self.fx * pc[:, 0] / z + self.cx
        v = self.fy * pc[:, 1] / z + self.cy
        u0, u1 = max(0, int(math.floor(u.min())) - 1), min(self.width, int(math.ceil(u.max())) + 2)
        v0, v1 = max(0, int(math.floor(v.min())) - 1), min(self.height, int(math.ceil(v.max())) + 2)
        if u0 >= u1 or v0 >= v1:
            return None
        return (v0, v1, u0, u1)

    def apply_noise(self, zbuf, mat, rng, sun=0.0):
        H, W = zbuf.shape
        hit = np.isfinite(zbuf)
        z = np.where(hit, zbuf, 0.0).astype(np.float32)
        sigma = (0.0015 + 0.0008 * z * z) * MAT_NOISE[mat] * (1.0 + 2.0 * sun)
        z += rng.standard_normal((H, W), dtype=np.float32) * sigma
        z[~hit] = 0.0
        p_drop = MAT_DROPOUT[mat]
        if sun > 0:
            p_drop = p_drop + sun * (0.04 + 0.5 * (z / self.max_depth) ** 2)
        z[rng.random((H, W), dtype=np.float32) < p_drop] = 0.0
        # flying pixels between foreground and background along depth edges
        a, b = z[:, :-1], z[:, 1:]
        dz = b - a
        edge = (np.abs(dz) > 0.2) & (a > 0) & (b > 0) & (a < self.max_depth * 1.5) & (b < self.max_depth * 1.5)
        edge &= rng.random(edge.shape, dtype=np.float32) < 0.35
        if edge.any():
            w = rng.random(int(edge.sum()), dtype=np.float32)
            b[edge] = a[edge] + dz[edge] * w
        z[(z < self.min_depth) | (z > self.max_depth)] = 0.0
        return z
