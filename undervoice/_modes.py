"""The two ways undervoice can carry data: under the voice, or below it (sub-audible)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import numpy as np

from . import _demodulator, _modulator, _subaudible, _waveform
from ._demodulator import Frame
from ._frames import Frames
from ._lpc import HOP
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


def mix(
    mode: str,
    speech: np.ndarray,
    message: Message,
    level_db: float,
    shape: float,
    pause_db: float | None = None,
    parts: Sequence[Part] = (),
) -> tuple[np.ndarray, list[Part]]:
    """A whole recording with the message parts and then message mixed in, and the parts that
    went out in it, which can be fewer than fit when the recording has long silences."""
    mod = mixer(mode, message, level_db, shape, pause_db)
    mod.frames.send(parts)
    if mode == SUB:
        mixed = mod.process(speech)
    else:
        # the voice modulator runs a hop behind, so run one more and drop the first
        mixed = mod.process(np.concatenate([speech, np.zeros(HOP)]))[HOP:]
    sent = []
    while not mod.frames.sent.empty():
        sent.append(mod.frames.sent.get_nowait())
    return mixed, sent


def encode(
    mode: str,
    speech: np.ndarray,
    message: Message,
    level_db: float,
    shape: float,
    pause_db: float | None = None,
    parts: Sequence[Part] = (),
) -> np.ndarray:
    return mix(mode, speech, message, level_db, shape, pause_db, parts)[0]


def decode(mode: str, audio: np.ndarray, shape: float) -> list[Frame]:
    if mode == SUB:
        return _subaudible.decode(audio)
    return _demodulator.decode(audio, shape)


def frame_length(mode: str) -> int:
    """Samples one frame takes in the mode."""
    return _subaudible.FRAME if mode == SUB else _waveform.FRAME
