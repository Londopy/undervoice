"""Receive side: find frames in audio and pull the bits out from under the speech."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import maximum_filter1d
from scipy.signal import correlate, fftconvolve, find_peaks, firwin, hilbert

from ._audio import resample_at
from ._fec import decode_bits
from ._lpc import HOP, match, whiten
from ._message import Payload, unpack
from ._modulator import SHAPE
from ._waveform import FRAME, FRAME_SYMBOLS, PILOTS, RATE, SYMBOL, reference

BLOCKS = 12
BLOCK = FRAME // BLOCKS
MAX_PPM = 200
# a block only counts towards timing if its pilots stand this far above the noise
BLOCK_DETECT = 4.0
PILOT_MASK = np.repeat(PILOTS, SYMBOL)
EDGES = [300, 700, 1200, 1900, 3000]
SUBBANDS = [firwin(101, [lo, hi], pass_zero=False, fs=RATE) for lo, hi in zip(EDGES, EDGES[1:])]


@dataclass(frozen=True)
class Frame:
    start: float
    message: Payload
    snr_db: float


def _reach(block: int) -> int:
    return int(np.ceil((block + 1) * BLOCK * MAX_PPM * 1e-6))


def _block_scores(xw: np.ndarray) -> list[np.ndarray]:
    # per block of pilots, normalized correlation against every possible frame start
    n = len(xw) - FRAME + 1
    ref = hilbert(reference())
    scores = []
    for b in range(BLOCKS):
        lo = b * BLOCK
        mask = PILOT_MASK[lo : lo + BLOCK].astype(float)
        tmpl = ref[lo : lo + BLOCK] * mask
        span = xw[lo : lo + n + BLOCK - 1]
        c = np.abs(correlate(span, tmpl, mode="valid", method="fft"))
        power = correlate(span**2, mask, mode="valid", method="fft") / mask.sum()
        scores.append(c / np.sqrt(np.maximum(power, 1e-20) * np.sum(tmpl.real**2)))
    return scores


def _candidates(scores: list[np.ndarray]) -> np.ndarray:
    # each block may sit a little early or late from clock drift, so take its best nearby score
    total = sum(maximum_filter1d(s, 2 * _reach(b) + 1) for b, s in enumerate(scores))
    peaks, _ = find_peaks(np.concatenate([[0.0], total, [0.0]]), distance=int(0.9 * FRAME))
    return peaks - 1


def _timing(tau: int, scores: list[np.ndarray]) -> tuple[float, float] | None:
    # fractional frame start and clock rate error, from where each block's pilots line up
    xs, ys, ws = [], [], []
    for b, s in enumerate(scores):
        lo, hi = max(tau - _reach(b), 0), min(tau + _reach(b) + 1, len(s))
        i = lo + int(np.argmax(s[lo:hi]))
        if s[i] < BLOCK_DETECT:
            continue
        frac = 0.0
        if 0 < i < len(s) - 1:
            y0, y1, y2 = s[i - 1 : i + 2]
            curve = y0 - 2 * y1 + y2
            if curve < 0:
                frac = 0.5 * (y0 - y2) / curve
        xs.append(b * BLOCK + BLOCK / 2)
        ys.append(i + frac)
        ws.append(s[i])
    if not xs:
        return None
    x, y, w = np.array(xs), np.array(ys), np.array(ws)
    while len(x) >= 3:
        alpha, start = np.polyfit(x, y, 1, w=w)
        # a weak block can lock onto a noise peak a few samples away; drop it and refit
        keep = np.abs(y - start - alpha * x) < 1.0
        if keep.all():
            if abs(alpha) <= 1.5 * MAX_PPM * 1e-6:
                return float(start), float(alpha)
            break
        if not keep.any():
            break
        x, y, w = x[keep], y[keep], w[keep]
    return float(np.average(y, weights=w)), 0.0


def _band(x: np.ndarray, taps: np.ndarray) -> np.ndarray:
    return fftconvolve(x, taps, mode="same")[HOP:].reshape(FRAME_SYMBOLS, SYMBOL)


def _mean(x: np.ndarray, width: int, lead: int | None = None) -> np.ndarray:
    # running mean over x[i - width + 1 + lead : i + lead + 1], centered by default, that
    # only averages what is there at the ends of the frame
    lead = (width - 1) // 2 if lead is None else lead
    ones = np.ones(width)
    total = np.convolve(x, ones)[lead : lead + len(x)]
    return total / np.convolve(np.ones(len(x)), ones)[lead : lead + len(x)]


def _soft(audio: np.ndarray, start: float, alpha: float, shape: float) -> tuple[np.ndarray, float]:
    # soft values for the frame's data symbols, and the mean pilot SNR in dB
    x = resample_at(audio, start + np.arange(-HOP, FRAME) * (1 + alpha))
    xw, coeffs = whiten(x)
    ref = match(np.concatenate([np.zeros(HOP), reference()]), coeffs, shape)
    ra = hilbert(ref)

    # each sub-band gets its own phase and signal level from the pilots, so bands where
    # channel noise drowns the data get little weight
    soft = np.zeros(int(np.count_nonzero(~PILOTS)))
    snr = 0.0
    for taps in SUBBANDS:
        xs = _band(xw, taps)
        rs = _band(ref, taps)
        z = np.sum(xs * np.conj(_band(ra, taps)), axis=1)
        z /= np.sqrt(np.mean(xs**2, axis=1) * np.sum(rs**2, axis=1)) + 1e-20
        phase = np.angle(np.convolve(z[PILOTS], np.ones(13), mode="same"))
        rotated = z * np.exp(-1j * np.repeat(phase, 4))
        y = rotated.real
        # once rotated the signal is all in the real part, so the imaginary part measures noise
        noise = _mean(rotated.imag**2, 25) + 1e-12
        # pilots either side of each group of three data symbols: the group after pilot g
        # sits between pilots g and g+1, so average g-1 to g+2
        level = _mean(y[PILOTS], 4, lead=2)
        level = np.repeat(np.maximum(level, 0.0), 4)
        soft += (2 * level * y / noise)[~PILOTS]
        snr += float(np.mean(level**2 / noise))
    return soft, 10 * np.log10(max(snr, 1e-6))


def decode(audio: np.ndarray, shape: float = SHAPE) -> list[Frame]:
    """Every frame that decodes cleanly in audio (mono, RATE), in order of start time."""
    if len(audio) < FRAME:
        return []
    xw, _ = whiten(audio)
    scores = _block_scores(xw)
    frames = []
    for tau in _candidates(scores):
        timing = _timing(int(tau), scores)
        if timing is None:
            continue
        soft, snr = _soft(audio, *timing, shape)
        message = unpack(decode_bits(soft))
        if message is not None:
            frames.append(Frame(timing[0], message, snr))
    return frames
