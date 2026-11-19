"""Frame layout and the spreading waveform shared by transmitter and receiver."""

from __future__ import annotations

from functools import cache

import numpy as np
from scipy.signal import firwin

RATE = 8000
SYMBOL = 100
FRAME_SYMBOLS = 744
FRAME = FRAME_SYMBOLS * SYMBOL
PILOTS = np.arange(FRAME_SYMBOLS) % 4 == 0


@cache
def reference() -> np.ndarray:
    """One frame of LFSR chips (x^17 + x^14 + 1), band-limited to 300-3000 Hz, unit RMS."""
    chips = np.empty(FRAME)
    state = 0x1FFFF
    for i in range(FRAME):
        chips[i] = 1.0 - 2.0 * ((state >> 16) & 1)
        state = ((state << 1) | (((state >> 16) ^ (state >> 13)) & 1)) & 0x1FFFF
    ref = np.convolve(chips, firwin(129, [300, 3000], pass_zero=False, fs=RATE), mode="same")
    ref /= np.sqrt(np.mean(ref**2))
    ref.flags.writeable = False
    return ref


def frame_wave(coded: np.ndarray) -> np.ndarray:
    """The unshaped data signal for one frame: pilots are +1, data symbols carry the coded bits."""
    signs = np.ones(FRAME_SYMBOLS)
    signs[~PILOTS] = 1.0 - 2.0 * coded
    return np.repeat(signs, SYMBOL) * reference()
