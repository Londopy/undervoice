"""Transmit side: hide frames under speech, one block at a time."""

from __future__ import annotations

import numpy as np
from scipy.signal import lfilter

from ._fec import encode_bits
from ._lpc import HOP, ORDER, bandwidth, lpc
from ._message import Message, pack
from ._waveform import FRAME, frame_wave

LEVEL_DB = -18.0
SHAPE = 0.5


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x**2)))


class Modulator:
    """Adds data to speech, shaped like it and level_db below it, block by block.
    Output lags input by one 20 ms hop."""

    def __init__(self, message: Message, level_db: float = LEVEL_DB, shape: float = SHAPE):
        if not 0.0 <= shape < 1.0:
            raise ValueError(f"shape must be from 0 up to (not including) 1, got {shape}")
        self.wave = frame_wave(encode_bits(pack(message)))
        self.ratio = 10 ** (level_db / 20)
        self.shape = shape
        self.pos = 0
        self.previous = np.zeros(HOP)
        self.zi = np.zeros(ORDER)
        self.gain = 0.0
        self.pending = np.zeros(0)
        self.ready = np.zeros(HOP)

    def restart(self) -> None:
        """Start the next hop at the beginning of a frame, keeping the audio already queued."""
        self.pos = 0
        self.zi = np.zeros(ORDER)
        self.gain = 0.0

    def process(self, speech: np.ndarray) -> np.ndarray:
        """Take any number of speech samples and return as many mixed samples."""
        self.pending = np.concatenate([self.pending, speech])
        hops = len(self.pending) // HOP
        done = [self._hop(self.pending[i * HOP : (i + 1) * HOP]) for i in range(hops)]
        self.pending = self.pending[hops * HOP :]
        self.ready = np.concatenate([self.ready, *done])
        out, self.ready = self.ready[: len(speech)], self.ready[len(speech) :]
        return out

    def _hop(self, speech: np.ndarray) -> np.ndarray:
        a = lpc(np.concatenate([self.previous, speech]))
        self.previous = speech
        data = self.wave[self.pos : self.pos + HOP]
        self.pos = (self.pos + HOP) % FRAME
        shaped, self.zi = lfilter([1.0], bandwidth(a, self.shape), data, zi=self.zi)
        gain = self.ratio * _rms(speech) / max(_rms(shaped), 1e-12)
        ramp = np.linspace(self.gain, gain, HOP, endpoint=False)
        self.gain = gain
        return speech + ramp * shaped


def encode(
    speech: np.ndarray, message: Message, level_db: float = LEVEL_DB, shape: float = SHAPE
) -> np.ndarray:
    """Mix repeating frames of message under a whole recording. Frames start at sample 0."""
    mod = Modulator(message, level_db, shape)
    return mod.process(np.concatenate([speech, np.zeros(HOP)]))[HOP:]
