"""Rate 1/2, K=7 convolutional code (171, 133 octal), Viterbi decoder and interleaver."""

from __future__ import annotations

import numpy as np

POLYS = (0o171, 0o133)
TAIL = 7
STRIDE = 37

# OUT[reg] = the two coded bits for a 7 bit register whose bit 6 is the newest input
OUT = np.array([[bin(reg & p).count("1") & 1 for p in POLYS] for reg in range(128)], np.uint8)

_STATE = np.arange(64)
_PREV0 = (_STATE & 31) << 1
_PREV1 = _PREV0 | 1
_SIGN0 = 1.0 - 2.0 * OUT[((_STATE >> 5) << 6) | _PREV0]
_SIGN1 = 1.0 - 2.0 * OUT[((_STATE >> 5) << 6) | _PREV1]


def _positions(n: int) -> np.ndarray:
    return (np.arange(n) * STRIDE) % n


def encode_bits(data: bytes) -> np.ndarray:
    """Encode bytes into interleaved coded bits (0/1), terminated with TAIL zero bits."""
    bits = np.concatenate([np.unpackbits(np.frombuffer(data, np.uint8)), np.zeros(TAIL, np.uint8)])
    coded = np.empty(2 * len(bits), np.uint8)
    state = 0
    for i, b in enumerate(bits):
        reg = (int(b) << 6) | state
        coded[2 * i : 2 * i + 2] = OUT[reg]
        state = reg >> 1
    out = np.empty_like(coded)
    out[_positions(len(coded))] = coded
    return out


def decode_bits(soft: np.ndarray) -> bytes:
    """Viterbi-decode interleaved soft values (positive means bit 0) back to bytes."""
    pairs = soft[_positions(len(soft))].reshape(-1, 2)
    metric = np.full(64, -np.inf)
    metric[0] = 0.0
    choice = np.empty((len(pairs), 64), bool)
    for t, pair in enumerate(pairs):
        m0 = metric[_PREV0] + _SIGN0 @ pair
        m1 = metric[_PREV1] + _SIGN1 @ pair
        choice[t] = m1 > m0
        metric = np.where(choice[t], m1, m0)
    bits = np.empty(len(pairs), np.uint8)
    state = 0
    for t in range(len(pairs) - 1, -1, -1):
        bits[t] = state >> 5
        state = ((state & 31) << 1) | int(choice[t, state])
    return np.packbits(bits[:-TAIL]).tobytes()
