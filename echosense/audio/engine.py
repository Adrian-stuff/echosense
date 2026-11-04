"""Real-time binaural audio engine.

The engine owns a sample clock. Every block (``render``) it asks its
generators (the obstacle sonifier, the training target) to schedule sounds,
mixes everything that overlaps the block, applies the master volume and a
peak limiter that enforces the output ceiling (ear safety, proposal C9), and
hands the block to the driver plus any taps (e.g. the browser stream).

Drivers
-------
* ``LocalDriver``  - PortAudio via ``sounddevice`` (speakers / headphones /
  bone-conduction headset on the Orange Pi's audio jack or USB/Bluetooth).
* ``ClockDriver``  - no audio hardware (Docker virtual device, CI): renders on
  a real-time clock so timing and latency logic behave exactly the same.
"""
from __future__ import annotations

import threading
import time

import numpy as np


def db_to_gain(db: float) -> float:
    return float(10 ** (db / 20))


class AudioEngine:
    def __init__(self, sample_rate: int = 48000, block_size: int = 1024, max_output_dbfs: float = -6.0):
        self.sr = int(sample_rate)
        self.block = int(block_size)
        self.ceiling = db_to_gain(max_output_dbfs)
        self.master_gain = 1.0
        self.muted = False
        self.clock = 0  # samples rendered so far
        self.output_latency = 0.0  # seconds from render to the speaker, set by the driver
        self.generators: list = []
        self.taps: list = []
        self._events: list[list] = []  # [start_sample, buffer(n,2)]
        self._lock = threading.Lock()
        self._lim_gain = 1.0
        self._render_wall = time.monotonic()
        self._render_clock = 0
        self.peak_dbfs = -120.0
        self.limiting = False
        self.underruns = 0
        self.render_load = 0.0  # fraction of real time spent rendering (audio-thread load)

    # ------------------------------------------------------------- timing
    def wall_time_of(self, sample: int) -> float:
        """Wall-clock (time.monotonic) at which ``sample`` reaches the listener."""
        return self._render_wall + (sample - self._render_clock) / self.sr + self.output_latency

    def now_sample(self) -> int:
        return self.clock

    # ---------------------------------------------------------- schedule
    def schedule(self, buf: np.ndarray, at_sample: int | None = None, gain: float = 1.0):
        if buf.ndim == 1:
            buf = np.stack([buf, buf], 1)
        start = self.clock if at_sample is None else max(int(at_sample), self.clock)
        with self._lock:
            self._events.append([start, (buf * gain).astype(np.float32)])

    def clear(self):
        with self._lock:
            self._events.clear()

    # ------------------------------------------------------------ render
    def render(self, n: int) -> np.ndarray:
        t_start = time.perf_counter()
        out = self._render(n)
        load = (time.perf_counter() - t_start) / (n / self.sr)
        self.render_load += 0.1 * (load - self.render_load)
        return out

    def _render(self, n: int) -> np.ndarray:
        start = self.clock
        self._render_wall, self._render_clock = time.monotonic(), start
        for gen in list(self.generators):
            try:
                gen(self, start, n)
            except Exception as e:  # never let a cue bug kill the audio thread
                print(f"[audio] generator error: {e!r}")
        out = np.zeros((n, 2), np.float32)
        end = start + n
        with self._lock:
            keep = []
            for ev in self._events:
                s0, buf = ev
                if s0 >= end:
                    keep.append(ev)
                    continue
                off = s0 - start  # >= 0 unless the event began in an earlier block
                b0 = max(0, -off)
                o0 = max(0, off)
                cnt = min(n - o0, buf.shape[0] - b0)
                if cnt > 0:
                    out[o0:o0 + cnt] += buf[b0:b0 + cnt]
                if b0 + cnt < buf.shape[0]:
                    keep.append(ev)
            self._events = keep
        out *= 0.0 if self.muted else self.master_gain
        # peak limiter: instant attack, ~150 ms release
        peak = float(np.abs(out).max()) if n else 0.0
        target = min(1.0, self.ceiling / peak) if peak > 0 else 1.0
        g0 = self._lim_gain
        g1 = target if target < g0 else min(target, g0 + (1 - g0) * min(1.0, n / (0.15 * self.sr)))
        if g0 != 1.0 or g1 != 1.0:
            out *= np.linspace(g0, g1, n, dtype=np.float32)[:, None] if g1 >= g0 else np.float32(g1)
        self._lim_gain = g1
        self.limiting = g1 < 0.999
        np.clip(out, -self.ceiling, self.ceiling, out=out)
        self.peak_dbfs = 20 * np.log10(max(1e-6, float(np.abs(out).max()))) if n else -120.0
        self.clock = end
        for tap in list(self.taps):
            try:
                tap(out)
            except Exception as e:
                print(f"[audio] tap error: {e!r}")
        return out


class ClockDriver:
    """Renders in real time without an audio device."""

    name = "none (clock only)"

    def __init__(self, engine: AudioEngine):
        self.engine = engine
        self._run = False
        self._th = None

    def start(self):
        self._run = True
        self.engine.output_latency = self.engine.block / self.engine.sr
        self._th = threading.Thread(target=self._loop, name="audio-clock", daemon=True)
        self._th.start()

    def _loop(self):
        e = self.engine
        period = e.block / e.sr
        nxt = time.monotonic()
        while self._run:
            e.render(e.block)
            nxt += period
            d = nxt - time.monotonic()
            if d > 0:
                time.sleep(d)
            elif d < -0.5:
                nxt = time.monotonic()

    def stop(self):
        self._run = False


class LocalDriver:
    name = "local audio device"

    def __init__(self, engine: AudioEngine, device=None):
        import sounddevice as sd  # imported lazily: needs PortAudio

        self.sd = sd
        self.engine = engine
        self.device = device
        self.stream = None

    def start(self):
        e = self.engine

        def cb(outdata, frames, _time, status):
            if status.output_underflow:
                e.underruns += 1
            outdata[:] = e.render(frames)

        self.stream = self.sd.OutputStream(samplerate=e.sr, blocksize=e.block, channels=2, dtype="float32",
                                           callback=cb, device=self.device, latency="low")
        self.stream.start()
        e.output_latency = float(self.stream.latency)
        try:
            self.name = f"local: {self.sd.query_devices(self.stream.device)['name']}"
        except Exception:
            pass

    def stop(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()


def make_driver(engine: AudioEngine, kind: str):
    if kind == "local":
        try:
            d = LocalDriver(engine)
            d.start()
            return d
        except Exception as e:  # no PortAudio / no device (Docker, headless CI)
            print(f"[audio] no local audio output ({e}); falling back to clock-only driver")
    d = ClockDriver(engine)
    d.start()
    return d


class BrowserTap:
    """Streams the binaural output (24 kHz int16 stereo) to subscribed web clients."""

    def __init__(self, engine: AudioEngine):
        self.engine = engine
        self.listeners: list = []  # callables(bytes)
        engine.taps.append(self)

    def __call__(self, block: np.ndarray):
        if not self.listeners:
            return
        if self.engine.sr == 48000:
            block = 0.5 * (block[0::2] + block[1::2])
        pcm = (np.clip(block, -1, 1) * 32767).astype("<i2").tobytes()
        for fn in list(self.listeners):
            fn(pcm)
