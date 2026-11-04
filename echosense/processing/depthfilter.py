"""Depth front-end: full-resolution sensor depth -> clean, downsampled depth for 3D projection.

Three steps, each aimed at a real depth-camera failure mode:

1. **Mixed ("flying") pixel removal.** At an object's silhouette a ToF pixel sees both
   the object and the background and reports a depth in between, which smears a
   ghost wall behind every edge. A pixel lying strictly between its left and right
   neighbours, far from both while they are far from each other, is dropped.
   libfreenect2's edge-aware filter does this on the Kinect v2. The simulator and
   the Kinect v1 need it here.
2. **Nearest-surface downsampling.** Plain striding (every 4th pixel) skips thin
   objects: a 5 cm pole at 4 m is only ~4 pixels wide. Each stride x stride block
   is instead reduced to the median of its *nearest surface* (the returns within
   ``near_layer`` of the block's second-nearest return). A thin pole covering two
   pixels of a block survives, a single noisy or flying pixel does not, and noise
   is averaged instead of being biased towards the sensor.
3. **Dropout hold.** Sunlight, dark or shiny surfaces make returns flicker on and
   off. A pixel that loses its return keeps its last value for ``hold_frames``
   frames (about 70 ms), so an obstacle doesn't blink out of the cue.
"""
from __future__ import annotations

import numpy as np


class DepthFrontEnd:
    def __init__(self, p: dict):
        self.stride = int(p["stride"])
        # lite boards: sample every Nth pixel first, then filter the smaller image (1/N^2 of the work)
        self.prestride = max(1, int(p.get("prestride", 1)))
        self.mode = p.get("downsample", "near_median")  # near_median | stride
        self.near_layer = float(p.get("near_layer", 0.25))
        self.hold_frames = int(p.get("hold_frames", 2))
        self.flying = bool(p.get("flying_pixel_filter", True))
        self.reset()

    def reset(self):
        self.prev = None
        self.age = None

    def __call__(self, depth: np.ndarray) -> np.ndarray:
        s = self.stride
        H, W = depth.shape
        h, w = H // s, W // s
        d = depth[: h * s, : w * s]
        ps = self.prestride if s % self.prestride == 0 else 1
        if ps > 1:
            d = d[::ps, ::ps]
            s //= ps
        if self.flying:
            d = remove_mixed_pixels(d)
        if self.mode == "stride" or s == 1:
            z = d[::s, ::s].copy()
        else:
            z = near_median_downsample(d, s, self.near_layer)
        if self.hold_frames > 0:
            if self.prev is not None and self.prev.shape == z.shape:
                fill = (z == 0) & (self.prev > 0) & (self.age < self.hold_frames)
                z = np.where(fill, self.prev, z)
                self.age = np.where(fill, self.age + 1, 0).astype(np.int8)
            else:
                self.age = np.zeros(z.shape, np.int8)
            self.prev = z
        return z


def remove_mixed_pixels(d: np.ndarray) -> np.ndarray:
    l, c, r = d[:, :-2], d[:, 1:-1], d[:, 2:]
    tol = 0.04 + 0.015 * c
    mixed = ((c > 0) & (l > 0) & (r > 0) & ((c - l) * (c - r) < 0)
             & (np.abs(c - l) > tol) & (np.abs(c - r) > tol) & (np.abs(l - r) > 3 * tol))
    if not mixed.any():
        return d
    out = d.copy()
    out[:, 1:-1][mixed] = 0.0
    return out


def near_median_downsample(d: np.ndarray, s: int, layer: float) -> np.ndarray:
    H, W = d.shape
    h, w = H // s, W // s
    blk = d.reshape(h, s, w, s).transpose(0, 2, 1, 3).reshape(h, w, s * s)
    blk = np.where(blk > 0, blk, np.float32(np.inf))
    blk.sort(axis=2)
    nvalid = np.isfinite(blk).sum(axis=2)
    ref = blk[:, :, 1]  # second-nearest: one stray pixel can't define the surface
    # values within `layer` of the reference form a prefix of the sorted block
    count = (blk <= (ref + layer)[:, :, None]).sum(axis=2)
    idx = np.clip((count - 1) // 2, 0, s * s - 1)
    z = np.take_along_axis(blk, idx[:, :, None], axis=2)[:, :, 0]
    return np.where(nvalid >= 2, z, 0.0).astype(np.float32)
