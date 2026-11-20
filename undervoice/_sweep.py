"""Frame loss against data level through the simulated FM channel."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import numpy as np
from scipy.stats import beta

from ._channel import fm_channel
from ._demodulator import decode
from ._message import Message
from ._modulator import encode
from ._waveform import FRAME


@dataclass(frozen=True)
class Result:
    level_db: float
    sent: int
    received: int
    wrong: int

    @property
    def loss(self) -> float:
        return 1 - self.received / self.sent if self.sent else 0.0

    @property
    def loss_upper(self) -> float:
        """The frame loss rate this run rules out with 95% confidence (one-sided
        Clopper-Pearson): with 0 lost out of 300 it is about 1%."""
        lost = self.sent - self.received
        if lost >= self.sent:
            return 1.0
        return float(beta.ppf(0.95, lost + 1, self.sent - lost))


def sweep(
    speech: Sequence[np.ndarray],
    levels: Sequence[float],
    message: Message,
    shape: float,
    trials: int,
    snr_db: float,
    hops: int,
    ppm: float,
    seed: int = 0,
    pause_db: float | None = None,
) -> Iterator[Result]:
    """For each level, send every recording `trials` times, each rotated to a random start
    so frames line up differently with the speech, and count frames that come back intact."""
    rng = np.random.default_rng(seed)
    for level in levels:
        sent = received = wrong = 0
        for clip in speech:
            for _ in range(trials):
                tx = encode(
                    np.roll(clip, int(rng.integers(len(clip)))), message, level, shape, pause_db
                )
                frames = decode(fm_channel(tx, snr_db, hops=hops, ppm=ppm, rng=rng), shape)
                good = sum(f.message == message for f in frames)
                sent += len(tx) // FRAME
                received += min(good, len(tx) // FRAME)
                wrong += len(frames) - good
        yield Result(level, sent, received, wrong)
