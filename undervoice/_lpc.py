"""Short-term LPC analysis, used to shape the data like speech and to whiten speech away."""

from __future__ import annotations

import numpy as np
from scipy.linalg import solve_toeplitz
from scipy.signal import lfilter

ORDER = 12
HOP = 160
WINDOW = np.hanning(2 * HOP)
_ONE = np.ones(1)


def lpc(segment: np.ndarray) -> np.ndarray:
    """Prediction filter A(z) for a 2*HOP sample segment."""
    w = segment * WINDOW
    r = np.array([w[: len(w) - k] @ w[k:] for k in range(ORDER + 1)])
    # white noise correction keeps silent and clipped segments well conditioned
    r[0] = r[0] * 1.0001 + 1e-12
    return np.concatenate([_ONE, solve_toeplitz(r[:ORDER], -r[1:])])


def bandwidth(a: np.ndarray, gamma: float) -> np.ndarray:
    """A(z/gamma): the same filter with its resonances flattened."""
    return a * gamma ** np.arange(len(a))


def analyze(x: np.ndarray) -> list[np.ndarray]:
    """A(z) for each HOP of x, from the hop and the one before it."""
    padded = np.concatenate([np.zeros(HOP), x, np.zeros(-len(x) % HOP)])
    return [lpc(padded[i : i + 2 * HOP]) for i in range(0, len(padded) - HOP, HOP)]


def _filter_hops(x: np.ndarray, filters: list[tuple[np.ndarray, np.ndarray]]) -> np.ndarray:
    out = np.empty_like(x)
    zi = np.zeros(ORDER)
    for i, (b, a) in enumerate(filters):
        hop = slice(i * HOP, (i + 1) * HOP)
        out[hop], zi = lfilter(b, a, x[hop], zi=zi)
    return out


def whiten(x: np.ndarray) -> tuple[np.ndarray, list[np.ndarray]]:
    """Filter x through its own A(z), leaving roughly white residual."""
    coeffs = analyze(x)
    return _filter_hops(x, [(a, _ONE) for a in coeffs]), coeffs


def match(x: np.ndarray, coeffs: list[np.ndarray], gamma: float) -> np.ndarray:
    """Filter x through A(z)/A(z/gamma): what whitening does to data shaped by 1/A(z/gamma)."""
    return _filter_hops(x, [(a, bandwidth(a, gamma)) for a in coeffs])
