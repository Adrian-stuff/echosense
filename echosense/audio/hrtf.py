"""Head-related transfer functions.

``StructuralHRTF`` implements the Brown & Duda (1998) structural model:

* spherical-head shadow (one-pole/one-zero filter whose high-frequency gain
  depends on the angle between the source and each ear),
* interaural time difference from the spherical-head delay formula,
* a five-echo pinna model that adds elevation-dependent spectral notches.

It needs no data files, runs on any device, and is parametric (head radius),
which makes it a good default for development. Measured HRTFs (e.g. a SOFA
file from the CIPIC/ARI/SADIE databases) can be swapped in later behind the
same ``hrir(az, el)`` interface.

Angles are in degrees: azimuth + = right of the user, elevation + = up.
"""
from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

C_SOUND = 343.0


class StructuralHRTF:
    name = "structural (Brown & Duda 1998)"

    def __init__(self, sample_rate: int = 48000, head_radius: float = 0.0875, taps: int = 256,
                 az_step: float = 2.0, el_step: float = 10.0):
        self.sr = int(sample_rate)
        self.a = float(head_radius)
        self.taps = int(taps)
        self.az_step, self.el_step = az_step, el_step
        self.nfft = 1024
        self.w = 2 * np.pi * np.fft.rfftfreq(self.nfft, 1.0 / self.sr)  # rad/s
        self._norm = 1.0
        h = self._compute(0.0, 0.0)
        self._norm = 1.0 / max(1e-9, float(np.sqrt((h ** 2).sum() / 2)))
        self._cached.cache_clear()

    def hrir(self, az: float, el: float = 0.0) -> np.ndarray:
        """(taps, 2) impulse response [left, right] for a source at (az, el), quantised to the grid."""
        az = max(-180.0, min(180.0, az))
        el = max(-40.0, min(60.0, el))
        qa = round(az / self.az_step) * self.az_step
        qe = round(el / self.el_step) * self.el_step
        return self._cached(qa, qe)

    @lru_cache(maxsize=2048)
    def _cached(self, az: float, el: float) -> np.ndarray:
        return (self._compute(az, el) * self._norm).astype(np.float32)

    def _compute(self, az: float, el: float) -> np.ndarray:
        a, w = self.a, self.w
        w0 = C_SOUND / a
        az_r, el_r = math.radians(az), math.radians(el)
        src = np.array([math.sin(az_r) * math.cos(el_r), math.sin(el_r), math.cos(az_r) * math.cos(el_r)])
        out = np.zeros((self.taps, 2))
        for ch, ear_x in ((0, -1.0), (1, 1.0)):
            theta = math.acos(max(-1.0, min(1.0, src[0] * ear_x)))  # angle source <-> ear axis
            alpha_min, theta_min = 0.1, math.radians(150)
            alpha = (1 + alpha_min / 2) + (1 - alpha_min / 2) * math.cos(theta / theta_min * math.pi)
            shadow = (1 + 1j * alpha * w / (2 * w0)) / (1 + 1j * w / (2 * w0))
            if theta < math.pi / 2:
                delay = -(a / C_SOUND) * math.cos(theta)
            else:
                delay = (a / C_SOUND) * (theta - math.pi / 2)
            delay += a / C_SOUND + 0.0005  # keep causal, leave room for the pinna echoes
            H = shadow * np.exp(-1j * w * delay)
            H = H * self._pinna(az if ear_x > 0 else -az, el)
            h = np.fft.irfft(H, self.nfft)[: self.taps]
            fade = np.ones(self.taps)
            n = self.taps // 4
            fade[-n:] = 0.5 * (1 + np.cos(np.linspace(0, np.pi, n)))
            out[:, ch] = h * fade
        return out

    def _pinna(self, az_ear: float, el: float) -> np.ndarray:
        rho = (0.5, -1.0, 0.5, -0.25, 0.25)
        A = (1, 5, 5, 5, 5)
        B = (2, 4, 7, 11, 13)
        D = (1.0, 0.5, 0.5, 0.5, 0.5)
        th, ph = math.radians(max(-90, min(90, az_ear))), math.radians(max(-90, min(90, el)))
        H = np.ones_like(self.w, dtype=complex)
        for r, a_, b_, d_ in zip(rho, A, B, D):
            tau = (a_ * math.cos(th / 2) * math.sin(d_ * (math.pi / 2 - ph)) + b_) / 44100.0
            H = H + r * np.exp(-1j * self.w * tau)
        return H / 1.5


def itd_ild(hrir: np.ndarray, sr: int) -> tuple[float, float]:
    """Interaural time (s, + = right ear leads) and level (dB, + = right louder) of an HRIR pair."""
    l, r = hrir[:, 0], hrir[:, 1]
    xc = np.correlate(l, r, "full")
    lag = int(np.argmax(np.abs(xc))) - (len(r) - 1)
    ild = 10 * math.log10((r ** 2).sum() / max(1e-12, (l ** 2).sum()))
    return lag / sr, ild
