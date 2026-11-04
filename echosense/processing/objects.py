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
            "fill", "density", "rough", "bottom_cut", "top_cut", "dist")


def shape_features(x, z, h, r, r_near, stride: int, fx: float, sensor_h: float, pitch_deg: float,
                   vfov_deg: float, top: float, cell: float = 0.1) -> np.ndarray:
    """Shape descriptor of one obstacle (body-frame points: x right, z forward, h height above floor)."""
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
    return np.array([x1 - x0, z1 - z0, h0, h1, h1 - h0, bands[0], bands[1], bands[2],
                     bands[2] / max(bands[1], 0.05), cells / box_cells, density, rough,
                     h0 - low_vis, top_vis - h1, r_near], np.float32)


def drop_label(h_min: float, width: float) -> str:
    return "step" if h_min > -0.3 and width > 1.0 else "hole"


# ------------------------------------------------------------------ decision tree
class DecisionTree:
    """CART classification tree (Gini), numpy only. Serialises to a small JSON file."""

    def __init__(self, max_depth=9, min_leaf=4, n_thresholds=24):
        self.max_depth, self.min_leaf, self.n_thr = max_depth, min_leaf, n_thresholds
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
            for f in range(X.shape[1]):
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
                    best = (float(score[j]), f, float(cand[j]))
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
        return {"type": "echosense-decision-tree", "version": 1, "classes": list(self.classes),
                "features": list(self.features), "max_depth": self.max_depth,
                "nodes": [[n[0], round(n[1], 5), n[2], n[3], [round(p, 4) for p in n[4]]] for n in self.nodes]}

    @classmethod
    def from_json(cls, d: dict) -> "DecisionTree":
        t = cls(max_depth=d.get("max_depth", 9))
        t.classes, t.features, t.nodes = tuple(d["classes"]), tuple(d["features"]), d["nodes"]
        return t


class ObjectRecognizer:
    def __init__(self, path: str | Path | None = None):
        p = Path(path) if path else MODEL_PATH
        self.tree = DecisionTree.from_json(json.loads(p.read_text())) if p.exists() else None
        self.path = p

    @property
    def available(self) -> bool:
        return self.tree is not None

    def proba(self, feats: np.ndarray) -> np.ndarray | None:
        return self.tree.proba(feats) if self.tree else None
