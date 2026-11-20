"""Setup checks: hear what the microphone would send, and key the radio briefly."""

from __future__ import annotations

import math
import queue
import time
from dataclasses import dataclass

import numpy as np
from scipy.signal import resample_poly

from ._audio import Decimator
from ._live import BLOCK, CARD_RATE, Device, _open, _sd
from ._message import Message
from ._modes import VOICE, decode, encode
from ._ptt import Keyer, SetupError
from ._waveform import FRAME, RATE

# a whole frame plus a second either side, so the check can decode one
SECONDS = FRAME / RATE + 2
PTT_SECONDS = 2.0


@dataclass(frozen=True)
class MicResult:
    peak_db: float
    speech_db: float
    frames: int
    advice: str
    mixed: np.ndarray


def _db(x: float) -> float:
    return 20 * math.log10(max(x, 1e-6))


def advise(audio: np.ndarray) -> tuple[float, float, str]:
    """(peak dBFS, speech level dBFS, what to do about it) for a microphone recording."""
    peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    hops = audio[: len(audio) // 160 * 160].reshape(-1, 160)
    # the level of the louder stretches, so pauses do not drag it down
    speech = float(np.percentile(np.sqrt(np.mean(hops**2, axis=1)), 90)) if len(hops) else 0.0
    speech_db = _db(speech)
    if peak >= 0.98:
        advice = "The microphone clips: turn its level down until the peaks stay below 0 dBFS."
    elif speech_db < -45:
        advice = "Almost nothing came from the microphone: check it is the right one and not muted."
    elif speech_db < -32:
        advice = "The microphone is quiet: turn its level up so speech peaks reach about -10 dBFS."
    else:
        advice = "The microphone level is good."
    return _db(peak), speech_db, advice


class MicCheck:
    """Records the microphone for SECONDS on its own thread; `peak` feeds a level meter. It is
    done early if the sound card stops, and then audio() says so."""

    def __init__(self, src: Device, seconds: float = SECONDS):
        self.wanted = int(seconds * RATE)
        self.peak = 0.0
        self._down = Decimator()
        self._blocks: queue.Queue[np.ndarray] = queue.Queue()
        self._got = 0
        sd = _sd()
        self._stream = _open(sd.InputStream, CARD_RATE, BLOCK, src, channels=1, callback=self._feed)
        self._stream.start()

    def _feed(self, indata, frames, when, status) -> None:
        if self._got >= self.wanted:
            return
        self.peak = float(np.max(np.abs(indata)))
        block = self._down(indata[:, 0].copy())
        self._got += len(block)
        self._blocks.put(block)

    @property
    def left(self) -> float:
        """Seconds still to record."""
        return max(self.wanted - self._got, 0) / RATE

    @property
    def done(self) -> bool:
        return self._got >= self.wanted or not self._stream.active

    def audio(self) -> np.ndarray:
        """The recording, once done; SetupError if the sound card stopped before the end."""
        self._stream.stop()
        self._stream.close()
        if self._got < self.wanted:
            raise SetupError(
                f"the microphone stopped after {self._got / RATE:.1f} s of the "
                f"{self.wanted / RATE:.0f} s recording"
            )
        blocks = []
        while not self._blocks.empty():
            blocks.append(self._blocks.get())
        return np.concatenate(blocks)[: self.wanted] if blocks else np.zeros(0)


def finish(
    audio: np.ndarray,
    message: Message,
    level_db: float,
    shape: float,
    pause_db: float | None,
    mode: str = VOICE,
) -> MicResult:
    """Mix the data into a microphone recording the way a transmission would, and decode it
    again in memory, which checks the level and the encoder and decoder at these settings."""
    peak_db, speech_db, advice = advise(audio)
    mixed = encode(mode, audio, message, level_db, shape, pause_db)
    frames = sum(f.message == message for f in decode(mode, mixed, shape))
    return MicResult(peak_db, speech_db, frames, advice, mixed)


def play_back(audio: np.ndarray, wait: bool = False) -> None:
    """Play RATE audio on the system's default output."""
    sd = _sd()
    _open(sd.play, resample_poly(audio, 6, 1).astype(np.float32), CARD_RATE, blocking=wait)


def key_briefly(keyer: Keyer, seconds: float = PTT_SECONDS) -> None:
    """Key the radio for a moment so the operator can see PTT work."""
    keyer.key(True)
    try:
        time.sleep(seconds)
    finally:
        keyer.key(False)
