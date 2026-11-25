"""Which way is up, from the sensor's accelerometer (Kinect v1).

The accelerometer measures gravity (plus the chest's own accelerations while walking) in the
sensor's axes. That gives the sensor's true tilt and roll every frame, whether or not any floor is
in view. The floor fit stays the main reference, because what matters for obstacles is height
above the surface underfoot, and a ramp is not level. Gravity:

* keeps heights right when no floor is visible (facing a wall up close, a crowd, a covered floor):
  before, the floor estimate went stale after 2 s and fell back to the configured mount,
* keeps the floor search to surfaces that are actually level (not a table edge or a steep ramp),
* tells the user when the sensor points at the floor or the sky (strap slipped, device dropped).

**Axis signs are learned, not assumed.** How libfreenect's accelerometer axes line up with the
depth image isn't documented, and a mirrored image flips one of them. Gravity is used only after
the up direction it gives has agreed with a well-fitted floor for long enough (``verified``). The
vertical axis is settled first, from which way gravity points when the sensor is roughly upright.
Until then the device works exactly as it does without an accelerometer.
"""
from __future__ import annotations

import math

import numpy as np

G = 9.80665


class GravityEstimator:
    def __init__(self, smoothing: float = 0.15, verify_frames: int = 20, min_agreement: float = 0.9):
        self.alpha = smoothing  # low-pass: walking accelerations average out, tilt changes come through
        self.verify_frames = verify_frames
        self.min_agreement = min_agreement
        self.reset()

    def reset(self):
        self.acc = None  # smoothed reading, sensor axes, m/s^2
        self.t = None
        self.signs = np.array([0.0, 0.0, 0.0])  # libfreenect axis -> camera axis sign (0 = not known yet)
        self._votes = {0: [0.0, 0, 0], 2: [0.0, 0, 0]}  # axis -> [weighted sum, agree, disagree]
        self.verified = False

    # ------------------------------------------------------------------ input
    def update(self, accel, t: float):
        a = np.asarray(accel, np.float64)
        n = float(np.linalg.norm(a))
        if not 0.5 * G < n < 1.6 * G:
            return  # a jolt, not gravity
        self.acc = a if self.acc is None else (1 - self.alpha) * self.acc + self.alpha * a
        self.t = t
        if self.signs[1] == 0 and abs(a[1]) > 0.7 * n:
            # camera y points down and the sensor is worn roughly upright: up is camera -y
            self.signs[1] = -math.copysign(1.0, a[1])

    def fresh(self, t: float) -> bool:
        return self.acc is not None and self.t is not None and t - self.t < 0.5

    def up_cam_raw(self) -> np.ndarray | None:
        """Up in camera axes (x right, y down, z forward) with the signs known so far (unknown axes 0)."""
        if self.acc is None or self.signs[1] == 0:
            return None
        u = self.acc * self.signs
        return u / max(np.linalg.norm(u), 1e-9)

    def up_cam(self, t: float) -> np.ndarray | None:
        """Up in camera axes, only once verified against the floor."""
        if not self.verified or not self.fresh(t):
            return None
        return self.up_cam_raw()

    # ------------------------------------------------------------ learning
    def learn_from_floor(self, floor_up_cam: np.ndarray):
        """Compare with the up direction a good floor fit gives (camera axes) and settle the x and z
        signs. An axis that carries almost no gravity (no roll, say) can't be learned and isn't needed."""
        if self.acc is None or self.signs[1] == 0:
            return
        a = self.acc / max(np.linalg.norm(self.acc), 1e-9)
        for ax in (0, 2):
            f, r = float(floor_up_cam[ax]), float(a[ax])
            if abs(f) < 0.05 or abs(r) < 0.05:
                continue  # under ~3 deg: no information about the sign
            v = self._votes[ax]
            agree = math.copysign(1.0, f) == math.copysign(1.0, r)
            v[0] += min(abs(f), abs(r)) * (1 if agree else -1)
            v[1 if agree else 2] += 1
            n = v[1] + v[2]
            if n >= self.verify_frames and max(v[1], v[2]) / n >= self.min_agreement:
                self.signs[ax] = 1.0 if v[1] > v[2] else -1.0
        # verified once z (pitch) is known; x (roll) is used when known, ignored otherwise
        if self.signs[2] != 0 and not self.verified:
            u = self.up_cam_raw()
            if u is not None and float(u @ floor_up_cam) > math.cos(math.radians(6)):
                self.verified = True

    # -------------------------------------------------------------- readout
    def angles(self, t: float) -> dict | None:
        u = self.up_cam(t)
        if u is None:
            return None
        return {"pitch": round(math.degrees(math.atan2(u[2], -u[1])), 1),
                "roll": round(math.degrees(math.atan2(-u[0], -u[1])), 1) if self.signs[0] else None}

    def status(self) -> str:
        if self.acc is None:
            return "none"
        return "verified" if self.verified else "learning"
