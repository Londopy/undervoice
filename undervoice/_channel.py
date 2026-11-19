"""A simulated narrowband FM voice path: radio audio filters, emphasis, limiter and noise."""

from __future__ import annotations

import numpy as np
from scipy.signal import bilinear, butter, lfilter, sosfilt

from ._audio import resample_at
from ._waveform import RATE

TX_HPF = butter(4, 300, "highpass", fs=RATE, output="sos")
SPLATTER = butter(6, 3000, "lowpass", fs=RATE, output="sos")
RX_HPF = butter(4, 300, "highpass", fs=RATE, output="sos")
RX_LPF = butter(4, 3000, "lowpass", fs=RATE, output="sos")
# 750 us emphasis; the second pole sits far up so it only keeps the digital filter proper
PRE = bilinear([750e-6, 1.0], [1 / (2 * np.pi * 20000), 1.0], fs=RATE)
DE = (PRE[1], PRE[0])


def _transmit(x: np.ndarray, drive_db: float) -> np.ndarray:
    x = lfilter(*PRE, sosfilt(TX_HPF, x))
    # mic gain set so speech peaks run drive_db into the deviation limiter
    peak = np.percentile(np.abs(x), 99.9)
    x = np.clip(x * 10 ** (drive_db / 20) / max(peak, 1e-12), -1.0, 1.0)
    return sosfilt(SPLATTER, x)


def _receive(x: np.ndarray) -> np.ndarray:
    return sosfilt(RX_LPF, sosfilt(RX_HPF, lfilter(*DE, x)))


def _noise(n: int, snr_db: float, rng: np.random.Generator) -> np.ndarray:
    # discriminator noise rises with frequency; scale it so a full-deviation 1 kHz tone at
    # the receiver output sits snr_db above it
    noise = np.diff(rng.standard_normal(n + 1))
    tone = _receive(np.sin(2 * np.pi * 1000 * np.arange(RATE) / RATE))[RATE // 2 :]
    out = _receive(noise)
    scale = np.sqrt(np.mean(tone**2) / np.mean(out**2) / 10 ** (snr_db / 10))
    return scale * noise


def fm_channel(
    audio: np.ndarray,
    snr_db: float = 30.0,
    drive_db: float = 3.0,
    hops: int = 1,
    ppm: float = 0.0,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Pass audio through `hops` FM links (1 = simplex, 2 = through a repeater).

    ppm offsets the receiving sound card's clock from the sending one.
    """
    rng = rng or np.random.default_rng()
    x = audio
    for _ in range(hops):
        sent = _transmit(x, drive_db)
        x = _receive(sent + _noise(len(sent), snr_db, rng))
    if ppm:
        step = 1 + ppm * 1e-6
        x = resample_at(x, np.arange(int(len(x) / step)) * step)
    return 0.5 * x / max(float(np.max(np.abs(x))), 1e-12)
