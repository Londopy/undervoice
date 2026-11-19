"""WAV files and sample rate conversion."""

from __future__ import annotations

from math import gcd
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import firwin, lfilter, resample_poly

from ._waveform import RATE

TAPS = 16


def read_wav(path: str | Path) -> np.ndarray:
    """Read a WAV file as mono float audio at RATE."""
    rate, x = wavfile.read(path)
    if x.dtype == np.uint8:
        x = (x.astype(np.float64) - 128) / 128
    elif np.issubdtype(x.dtype, np.integer):
        x = x / float(np.iinfo(x.dtype).max + 1)
    x = np.asarray(x, np.float64)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if rate != RATE:
        g = gcd(rate, RATE)
        x = resample_poly(x, RATE // g, rate // g)
    return x


def to_int16(x: np.ndarray) -> np.ndarray:
    return (np.clip(x, -1.0, 1.0) * 32767).astype(np.int16)


def write_wav(path: str | Path, x: np.ndarray) -> None:
    """Write 16-bit mono audio at RATE."""
    wavfile.write(path, RATE, to_int16(x))


def resample_at(x: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Values of x at fractional sample times t, by windowed sinc interpolation."""
    padded = np.concatenate([np.zeros(TAPS), x, np.zeros(TAPS + 1)])
    k = np.arange(-TAPS + 1, TAPS + 1)
    out = np.empty(len(t))
    # chunked so a long file does not need a len(t) x 32 array all at once
    for start in range(0, len(t), 65536):
        tt = t[start : start + 65536]
        base = np.floor(tt).astype(np.int64)
        d = k - (tt - base)[:, None]
        w = np.sinc(d) * (0.5 + 0.5 * np.cos(np.pi * d / TAPS))
        idx = np.clip(base[:, None] + k + TAPS, 0, len(padded) - 1)
        out[start : start + len(tt)] = np.sum(padded[idx] * w, axis=1)
    return out


class Decimator:
    """48 kHz to RATE, block by block, for blocks that are a multiple of 6 samples."""

    def __init__(self) -> None:
        self.h = firwin(121, 3600, fs=6 * RATE)
        self.zi = np.zeros(len(self.h) - 1)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y, self.zi = lfilter(self.h, 1.0, x, zi=self.zi)
        return y[::6]


class Interpolator:
    """RATE to 48 kHz, block by block."""

    def __init__(self) -> None:
        self.h = 6 * firwin(121, 3600, fs=6 * RATE)
        self.zi = np.zeros(len(self.h) - 1)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        up = np.zeros(6 * len(x))
        up[::6] = x
        y, self.zi = lfilter(self.h, 1.0, up, zi=self.zi)
        return y
