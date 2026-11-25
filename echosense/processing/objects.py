"""Lightweight object recognition from depth alone.

Each obstacle the pipeline finds is described by ~15 shape numbers (size, height profile,
how solid and how smooth its surface is, whether the field of view cuts it off). A small
decision tree (a few KB of JSON, microseconds per obstacle, pure numpy, no GPU, no camera)
maps them to a class. A tree is used because it is cheap enough for an Orange Pi One and easy
to inspect. Xu, Song & Wang (2023, in the proposal's related studies) report a 53 KB decision
tree at 98.7 % on a Raspberry Pi.

Classes: person, pole, vehicle, wall, object (stalls, boxes, cases, bins), low (curbs,
benches), overhang (signs, cables, branches, awnings). Drop-offs are named by rule (hole vs
step down) from their depth.

The model ships trained on simulator scenes (``echosense train-objects``). Shapes in a
simulator are cleaner than real ones, so it must be re-validated, and ideally retrained, on
labelled Kinect recordings from the field (the trainer accepts extra CSV rows).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

MODEL_PATH = Path(__file__).resolve().parent / "models" / "objects.json"
CLASSES = ("person", "pole", "vehicle", "wall", "object", "low", "overhang")
SPOKEN = {"person": "Person", "pole": "Pole", "vehicle": "Vehicle", "wall": "Wall", "object": "Object",
          "low": "Low obstacle", "overhang": "Overhang", "hole": "Hole", "step": "Step down",
          "curb": "Curb", "unknown": "Obstacle"}

FEATURES = ("width", "depth", "h_min", "h_max", "h_span", "w_low", "w_mid", "w_high", "top_ratio",
            "fill", "density", "rough", "bottom_cut", "top_cut", "dist", "side_cut", "flat", "oblique")


def shape_features(x, z, h, r, r_near, stride: int, fx: float, sensor_h: float, pitch_deg: float,
                   vfov_deg: float, top: float, cell: float = 0.1, hfov_deg: float | None = None) -> np.ndarray:
    """Shape descriptor of one obstacle (body-frame points: x right, z forward, h height above floor).

    Besides size and height profile: ``bottom_cut`` / ``top_cut`` / ``side_cut`` say how far the object
    is from the edges of the field of view (near zero = cut off there, so its true size is unknown:
    half a wall is not a person); ``flat`` is how far the visible surface, seen from above, is from a
    straight line (wall, box face ~0; a person or a pole is curved); ``oblique`` is how much that
    surface runs along the line of sight rather than facing the user (a wall seen at an angle)."""
    x0, x1 = np.percentile(x, (2, 98))
    z0, z1 = np.percentile(z, (2, 98))
    face = r <= r_near + 0.35
    hf = h[face] if face.sum() >= 4 else h
    h0, h1 = np.percentile(hf, (3, 97))
    bands = []
    for lo, hi in ((0.0, 0.6), (0.6, 1.2), (1.2, 2.0)):
        m = (h >= lo) & (h < hi)
        bands.append(float(np.percentile(x[m], 95) - np.percentile(x[m], 5)) if m.sum() >= 3 else 0.0)
    ix = ((x - x0) / cell).astype(np.int32)
    iz = ((z - z0) / cell).astype(np.int32)
    ok = (ix >= 0) & (iz >= 0)
    cells = np.unique(ix[ok] * 4096 + iz[ok]).size
    box_cells = max(1, (int((x1 - x0) / cell) + 1) * (int((z1 - z0) / cell) + 1))
    # samples per square metre of visible surface, normalised so a solid wall at any distance is ~1
    per_m2 = (fx / stride) ** 2 / max(np.mean(r) ** 2, 0.09)
    area = max(0.02, (x1 - x0 + 0.05) * (h1 - h0 + 0.05))
    density = min(3.0, x.size / (per_m2 * area))
    rough = float(np.median(np.abs(r[face] - np.median(r[face])))) if face.sum() >= 4 else 0.0
    lo_ang = math.radians(pitch_deg - vfov_deg / 2)
    hi_ang = math.radians(pitch_deg + vfov_deg / 2)
    low_vis = max(0.0, sensor_h + r_near * math.tan(lo_ang))
    top_vis = min(top, sensor_h + r_near * math.tan(hi_ang))
    az = np.degrees(np.arctan2(np.abs(x), np.maximum(z, 0.05)))
    side = (hfov_deg / 2 - float(np.percentile(az, 98))) if hfov_deg else 30.0
    xf, zf = (x[face], z[face]) if face.sum() >= 6 else (x, z)
    flat, obl = 0.0, 0.0
    if xf.size >= 6:
        c = np.cov(np.stack([xf, zf]))
        ev, evec = np.linalg.eigh(c + np.eye(2) * 1e-9)
        flat = float(np.sqrt(max(ev[0], 0.0) / max(ev[1], 1e-9)))  # 0 = a straight line from above
        ray = np.array([np.mean(xf), np.mean(zf)])
        ray /= max(np.linalg.norm(ray), 1e-6)
        obl = float(abs(evec[:, 1] @ ray))  # 1 = surface runs along the line of sight
    return np.array([x1 - x0, z1 - z0, h0, h1, h1 - h0, bands[0], bands[1], bands[2],
                     bands[2] / max(bands[1], 0.05), cells / box_cells, density, rough,
                     h0 - low_vis, top_vis - h1, r_near, side, flat, obl], np.float32)


def drop_label(h_min: float, width: float) -> str:
    return "step" if h_min > -0.3 and width > 1.0 else "hole"


# ------------------------------------------------------------------ decision tree
class DecisionTree:
    """CART classification tree (Gini), numpy only. Serialises to a small JSON file."""

    def __init__(self, max_depth=9, min_leaf=4, n_thresholds=24, max_features=None, seed=0):
        self.max_depth, self.min_leaf, self.n_thr = max_depth, min_leaf, n_thresholds
        self.max_features = max_features  # random subset of features tried at each split (forests)
        self.rng = np.random.default_rng(seed)
        self.nodes: list = []  # [feature, threshold, left, right, probs]
        self.classes: tuple = CLASSES
        self.features: tuple = FEATURES

    def fit(self, X: np.ndarray, y: np.ndarray, weights: np.ndarray | None = None):
        self.nodes = []
        k = len(self.classes)
        w = np.ones(len(y)) if weights is None else weights

        def leaf(idx):
            counts = np.bincount(y[idx], weights=w[idx], minlength=k)
            return counts / max(counts.sum(), 1e-9)

        def gini(counts):
            tot = counts.sum(-1, keepdims=True)
            p = counts / np.maximum(tot, 1e-9)
            return 1.0 - (p * p).sum(-1)

        def build(idx, depth):
            node_id = len(self.nodes)
            self.nodes.append([-1, 0.0, -1, -1, leaf(idx).tolist()])
            if depth >= self.max_depth or idx.size < 2 * self.min_leaf or len(np.unique(y[idx])) == 1:
                return node_id
            onehot = np.zeros((idx.size, k))
            onehot[np.arange(idx.size), y[idx]] = w[idx]
            total = onehot.sum(0)
            parent = gini(total[None])[0] * total.sum()
            best = (0.0, None, None)
            feats = range(X.shape[1])
            if self.max_features and self.max_features < X.shape[1]:
                feats = self.rng.choice(X.shape[1], self.max_features, replace=False)
            for f in feats:
                v = X[idx, f]
                order = np.argsort(v, kind="stable")
                vs, cum = v[order], np.cumsum(onehot[order], 0)
                cand = np.unique(np.quantile(vs, np.linspace(0.02, 0.98, self.n_thr)))
                pos = np.searchsorted(vs, cand, side="right")
                ok = (pos >= self.min_leaf) & (pos <= idx.size - self.min_leaf)
                if not ok.any():
                    continue
                pos, cand = pos[ok], cand[ok]
                left = cum[pos - 1]
                right = total - left
                score = parent - gini(left) * left.sum(1) - gini(right) * right.sum(1)
                j = int(np.argmax(score))
                if score[j] > best[0] + 1e-9:
                    best = (float(score[j]), int(f), float(cand[j]))
            if best[1] is None:
                return node_id
            f, thr = best[1], best[2]
            go_left = X[idx, f] <= thr
            l_id = build(idx[go_left], depth + 1)
            r_id = build(idx[~go_left], depth + 1)
            self.nodes[node_id][:4] = [f, thr, l_id, r_id]
            return node_id

        build(np.arange(len(y)), 0)
        return self

    def proba(self, x) -> np.ndarray:
        n = self.nodes[0]
        while n[0] >= 0:
            n = self.nodes[n[2] if x[n[0]] <= n[1] else n[3]]
        return np.asarray(n[4], np.float32)

    def predict(self, X) -> np.ndarray:
        return np.array([int(np.argmax(self.proba(x))) for x in X])

    def to_json(self) -> dict:
        def leaf(n):  # inner nodes don't need their class distribution
            return [round(p, 3) for p in n[4]] if n[0] < 0 else 0
        return {"type": "echosense-decision-tree", "version": 1, "classes": list(self.classes),
                "features": list(self.features), "max_depth": self.max_depth,
                "nodes": [[n[0], round(n[1], 5), n[2], n[3], leaf(n)] for n in self.nodes]}

    @classmethod
    def from_json(cls, d: dict) -> "DecisionTree":
        t = cls(max_depth=d.get("max_depth", 9))
        t.classes, t.features, t.nodes = tuple(d["classes"]), tuple(d["features"]), d["nodes"]
        return t


class Forest:
    """A few small trees on bootstrap samples, probabilities averaged: steadier and better calibrated
    than one deep tree (one tree's leaf says 100 % far too often), still microseconds per obstacle."""

    def __init__(self, trees: list):
        self.trees = trees
        self.classes = trees[0].classes
        self.features = trees[0].features

    @classmethod
    def fit(cls, X, y, weights=None, n_trees=9, max_depth=8, min_leaf=6, seed=0):
        rng = np.random.default_rng(seed)
        trees = []
        k = max(4, int(round(0.6 * X.shape[1])))
        for i in range(n_trees):
            idx = rng.integers(0, len(y), len(y))
            w = None if weights is None else weights[idx]
            trees.append(DecisionTree(max_depth=max_depth, min_leaf=min_leaf, max_features=k, seed=seed + i)
                         .fit(X[idx], y[idx], w))
        return cls(trees)

    def proba(self, x) -> np.ndarray:
        return np.mean([t.proba(x) for t in self.trees], axis=0).astype(np.float32)

    def predict(self, X) -> np.ndarray:
        return np.array([int(np.argmax(self.proba(x))) for x in X])

    @property
    def nodes(self):
        return [n for t in self.trees for n in t.nodes]

    def to_json(self) -> dict:
        return {"type": "echosense-forest", "version": 1, "classes": list(self.classes),
                "features": list(self.features), "trees": [t.to_json() for t in self.trees]}

    @classmethod
    def from_json(cls, d: dict) -> "Forest":
        return cls([DecisionTree.from_json(t) for t in d["trees"]])


def load_model(d: dict):
    return Forest.from_json(d) if d.get("type") == "echosense-forest" else DecisionTree.from_json(d)


class ObjectRecognizer:
    def __init__(self, path: str | Path | None = None):
        p = Path(path) if path else MODEL_PATH
        self.tree = load_model(json.loads(p.read_text())) if p.exists() else None
        self.path = p
        # a model trained before newer features were added only reads the first ones
        self.n_features = len(self.tree.features) if self.tree is not None else len(FEATURES)

    @property
    def available(self) -> bool:
        return self.tree is not None

    def proba(self, feats: np.ndarray) -> np.ndarray | None:
        return self.tree.proba(feats[: self.n_features]) if self.tree else None
