"""Spoken confirmations and warnings (proposal: controls, status, battery, fail-safe).

Text is synthesised to a WAV in memory and mixed into the audio engine, so
speech reaches every output (local device, browser stream) and ducks under
nothing. Back-ends, tried in order: ``espeak-ng`` (Orange Pi / Linux),
``say`` (macOS), then a tonal fallback when no TTS is installed.
"""
from __future__ import annotations

import io
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import wave

import numpy as np

from .engine import AudioEngine
from .sonify import earcon


def _read_wav(data: bytes) -> tuple[np.ndarray, int]:
    try:
        with wave.open(io.BytesIO(data)) as w:
            sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError):  # espeak's streamed header has bogus sizes
        i = data.find(b"data")
        sr, ch, sw, raw = 22050, 1, 2, data[i + 8:] if i >= 0 else b""
    if sw != 2:
        raise ValueError("expected 16-bit wav")
    x = np.frombuffer(raw[: len(raw) // 2 * 2], "<i2").astype(np.float32) / 32768
    if ch > 1:
        x = x.reshape(-1, ch).mean(1)
    return x, sr


class Speech:
    def __init__(self, engine: AudioEngine, cfg: dict, on_say=None):
        self.engine = engine
        self.rate = int(cfg.get("rate", 175))
        self.voice = cfg.get("voice", "")
        self.on_say = on_say
        self.backend = self.detect_backend(cfg.get("engine", "auto"))
        self.gain = 0.55
        self._cache: dict[str, np.ndarray] = {}
        self._q: queue.Queue = queue.Queue()
        threading.Thread(target=self._worker, name="speech", daemon=True).start()

    @staticmethod
    def detect_backend(pref: str = "auto") -> str:
        if pref in ("none", "tones"):
            return "tones"
        for b in (["espeak-ng", "espeak"] if pref in ("auto", "espeak") else []):
            if shutil.which(b):
                return b
        if pref in ("auto", "say") and sys.platform == "darwin" and shutil.which("say"):
            return "say"
        return "tones"

    def say(self, text: str, earcon_name: str | None = None):
        if self.on_say:
            self.on_say(text)
        self._q.put((text, earcon_name))

    def _worker(self):
        while True:
            text, ec = self._q.get()
            try:
                if ec:
                    self.engine.schedule(earcon(ec, self.engine.sr))
                pcm = self._synth(text)
                if pcm is not None:
                    self.engine.schedule(pcm, at_sample=self.engine.now_sample() + (int(0.25 * self.engine.sr) if ec else 0),
                                         gain=self.gain)
            except Exception as e:
                print(f"[speech] {e!r}")

    def _synth(self, text: str) -> np.ndarray | None:
        if text in self._cache:
            return self._cache[text]
        if self.backend == "tones":
            return None
        if self.backend in ("espeak-ng", "espeak"):
            cmd = [self.backend, "-s", str(self.rate), "--stdout"]
            if self.voice:
                cmd += ["-v", self.voice]
            data = subprocess.run(cmd + [text], capture_output=True, timeout=10).stdout
        else:
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            try:
                cmd = ["say", "-r", str(self.rate), "-o", path, "--data-format=LEI16@22050"]
                if self.voice:
                    cmd += ["-v", self.voice]
                subprocess.run(cmd + [text], capture_output=True, timeout=10)
                data = open(path, "rb").read()
            finally:
                os.unlink(path)
        x, sr = _read_wav(data)
        if sr != self.engine.sr and x.size:
            n = int(x.size * self.engine.sr / sr)
            x = np.interp(np.linspace(0, x.size - 1, n), np.arange(x.size), x).astype(np.float32)
        self._cache[text] = x
        return x
