"""Transmit side: hide frames under speech, one block at a time."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from scipy.signal import lfilter

from ._fec import encode_bits
from ._frames import Frames
from ._lpc import HOP, ORDER, bandwidth, lpc
from ._message import Message, Part
from ._waveform import FRAME, frame_wave

LEVEL_DB = -21.0
SHAPE = 0.5
# how fast the remembered speech level falls during a pause: about 4 dB in ten seconds
RECENT_FADE = 0.999
# a message part counts as sent only if its frame carried at least this much data power re full
# scale: speech at -27 dBFS clears it by about 18 dB, while silence, or mic hiss at -50 dBFS
# under a keyed but silent mic, falls short
SENT_DB = -65.0
# and only if the data was spread over the frame, since the receiver locks on across all of it:
# SPREAD of its STRETCHES (0.62 s each) within SPREAD_DB of the strongest. Speech with ordinary
# pauses covers 70% or more; a few seconds of speech and then silence, which doesn't decode, 40%
STRETCHES = 15
SPREAD_DB = 20.0
SPREAD = 0.6


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x**2)))


class Modulator:
    """Adds data to speech, shaped like it and level_db below it, block by block.
    Output lags input by one 20 ms hop. Message parts given to frames.send() go out next; one
    whose frame went out with too little speech to carry it (SENT_DB, SPREAD) goes again.

    With pause_db set, the data carries on through pauses at pause_db below the recent speech
    level instead of stopping, trading a faint hiss in the gaps for frames that survive them.
    It can't be above level_db, or the data would rise whenever the speech got quieter."""

    def __init__(
        self,
        message: Message,
        level_db: float = LEVEL_DB,
        shape: float = SHAPE,
        pause_db: float | None = None,
    ):
        if not 0.0 <= shape < 1.0:
            raise ValueError(f"shape must be from 0 up to (not including) 1, got {shape}")
        if pause_db is not None and pause_db > level_db:
            raise ValueError(
                f"the pause level ({pause_db:g} dB) can't be above the data level ({level_db:g} dB)"
            )
        self.frames = Frames(message, lambda payload: frame_wave(encode_bits(payload)))
        self.wave = np.zeros(FRAME)
        self.carried = np.zeros(STRETCHES)
        self.ratio = 10 ** (level_db / 20)
        self.pause_ratio = 0.0 if pause_db is None else 10 ** (pause_db / 20)
        self.recent = 0.0
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
        self.carried = np.zeros(STRETCHES)
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
        if self.pos == 0:
            self.wave = self.frames.start()
        data = self.wave[self.pos : self.pos + HOP]
        stretch = self.pos * STRETCHES // FRAME
        self.pos = (self.pos + HOP) % FRAME
        shaped, self.zi = lfilter([1.0], bandwidth(a, self.shape), data, zi=self.zi)
        level = _rms(speech)
        self.recent = max(level, self.recent * RECENT_FADE)
        target = max(self.ratio * level, self.pause_ratio * self.recent)
        gain = target / max(_rms(shaped), 1e-12)
        added = np.linspace(self.gain, gain, HOP, endpoint=False) * shaped
        self.gain = gain
        self.carried[stretch] += float(added @ added)
        if self.pos == 0:
            self._end_frame()
        return speech + added

    def _end_frame(self) -> None:
        # a part left unfinished goes again from the next frame's start()
        power = self.carried * STRETCHES / FRAME
        spread = np.mean(power >= np.max(power) * 10 ** (-SPREAD_DB / 10))
        if power.mean() >= 10 ** (SENT_DB / 10) and spread >= SPREAD:
            self.frames.finish()
        self.carried = np.zeros(STRETCHES)


def encode(
    speech: np.ndarray,
    message: Message,
    level_db: float = LEVEL_DB,
    shape: float = SHAPE,
    pause_db: float | None = None,
    parts: Sequence[Part] = (),
) -> np.ndarray:
    """Mix frames under a whole recording, starting at sample 0: the message parts, then
    message over and over."""
    mod = Modulator(message, level_db, shape, pause_db)
    mod.frames.send(parts)
    return mod.process(np.concatenate([speech, np.zeros(HOP)]))[HOP:]
