"""The two ways undervoice can carry data: under the voice, or below it (sub-audible)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import numpy as np

from . import _demodulator, _modulator, _subaudible
from ._demodulator import Frame
from ._frames import Frames
from ._message import Message, Part

VOICE = "voice"
SUB = "sub"
MODES = (VOICE, SUB)


class Mixer(Protocol):
    frames: Frames

    def process(self, speech: np.ndarray) -> np.ndarray: ...


def mixer(
    mode: str, message: Message, level_db: float, shape: float, pause_db: float | None
) -> Mixer:
    """A streaming modulator for the mode. Shape and pause level only apply under the voice."""
    if mode == SUB:
        return _subaudible.Modulator(message, level_db)
    return _modulator.Modulator(message, level_db, shape, pause_db)


def encode(
    mode: str,
    speech: np.ndarray,
    message: Message,
    level_db: float,
    shape: float,
    pause_db: float | None = None,
    parts: Sequence[Part] = (),
) -> np.ndarray:
    if mode == SUB:
        return _subaudible.encode(speech, message, level_db, parts)
    return _modulator.encode(speech, message, level_db, shape, pause_db, parts)


def decode(mode: str, audio: np.ndarray, shape: float) -> list[Frame]:
    if mode == SUB:
        return _subaudible.decode(audio)
    return _demodulator.decode(audio, shape)
