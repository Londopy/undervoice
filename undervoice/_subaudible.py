"""Sub-audible mode: DBPSK on a 150 Hz carrier below the voice band, where squelch tones live."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import (
    bilinear,
    correlate,
    ellip,
    fftconvolve,
    find_peaks,
    firwin,
    freqz,
    lfilter,
    sosfilt,
)

from ._demodulator import Frame
from ._fec import decode_bits, encode_bits
from ._frames import Frames
from ._message import Message, Part, unpack
from ._waveform import RATE

CARRIER = 150
SYMBOL = 80
# the rotation of a 31 chip m-sequence with the lowest aperiodic sidelobes (4)
SYNC = 1.0 - 2.0 * np.array([int(b) for b in "1100011111001101001000010101110"])
FRAME_SYMBOLS = len(SYNC) + 558
FRAME = FRAME_SYMBOLS * SYMBOL
LEVEL_DB = -18.0

# each symbol is a root-raised-cosine pulse, roll-off 1, that peaks DELAY samples after the
# symbol starts and runs as long again, scaled so random symbols come out at unit power
DELAY = 4 * SYMBOL
_T = np.arange(-DELAY, DELAY + 1) / SYMBOL
PULSE = np.sinc(2 * _T - 0.5) + np.sinc(2 * _T + 0.5)
PULSE *= np.sqrt(SYMBOL / np.sum(PULSE**2))
# steeper than a radio's mic filter, so the voice's fundamental stays off the carrier
SPEECH_HPF = ellip(8, 0.5, 60, 300, "highpass", fs=RATE, output="sos")
LOWPASS = firwin(529, 275, fs=RATE)
# the data port skips the radio's 750 us pre-emphasis, so the voice gets it here instead, or
# ordinary receivers' de-emphasis would leave it dull. With the pole at 3.5 kHz the digital
# filter follows the analog one within 0.2 dB from 300 Hz to 3 kHz, then levels off; unity
# gain at 1 kHz keeps the voice's level
_B, _A = bilinear([750e-6, 1.0], [1 / (2 * np.pi * 3500), 1.0], fs=RATE)
EMPHASIS = (_B / abs(freqz(_B, _A, worN=[1000], fs=RATE)[1][0]), _A)
# a frame the error correction can still decode scores 0.55 or more, and random data rarely
# passes 0.5; a false peak only costs a failed decode
SYNC_DETECT = 0.45
# symbols per timing estimate, 19 to the frame
TIMING_BLOCK = 31


def _chips(payload: bytes) -> np.ndarray:
    # one per symbol: +1 keeps the carrier phase, -1 flips it
    return np.concatenate([SYNC, 1.0 - 2.0 * encode_bits(payload)])


class Modulator:
    """Adds frames of message to speech at a constant level_db, the data's power relative to a
    full-scale sine, block by block with no delay. The speech is high-passed at 300 Hz and
    pre-emphasized, since through the data port the radio does neither. Message parts given
    to frames.send() go out next."""

    def __init__(self, message: Message, level_db: float = LEVEL_DB):
        self.frames = Frames(message, _chips)
        self.chips = np.zeros(FRAME_SYMBOLS)
        self.amplitude = 10 ** (level_db / 20)
        self.n = 0
        self.k = 0
        self.sign = 1.0
        self.pulse_zi = np.zeros(len(PULSE) - 1)
        self.speech_zi = np.zeros((len(SPEECH_HPF), 2))
        self.emphasis_zi = np.zeros(1)

    def process(self, speech: np.ndarray) -> np.ndarray:
        """Take any number of speech samples and return as many mixed samples."""
        # lfilter's FIR path cannot take an empty block
        if not len(speech):
            return np.zeros(0)
        n = self.n + np.arange(len(speech))
        self.n += len(speech)
        impulses = np.zeros(len(speech))
        for i in np.flatnonzero(n % SYMBOL == 0):
            if self.k == 0:
                self.chips = self.frames.start()
            self.sign *= self.chips[self.k]
            self.k = (self.k + 1) % FRAME_SYMBOLS
            if self.k == 0:
                self.frames.finish()
            impulses[i] = self.sign
        data, self.pulse_zi = lfilter(PULSE, 1.0, impulses, zi=self.pulse_zi)
        voice, self.speech_zi = sosfilt(SPEECH_HPF, speech, zi=self.speech_zi)
        voice, self.emphasis_zi = lfilter(*EMPHASIS, voice, zi=self.emphasis_zi)
        return voice + self.amplitude * data * np.cos(2 * np.pi * CARRIER * n / RATE)


def encode(
    speech: np.ndarray, message: Message, level_db: float = LEVEL_DB, parts: Sequence[Part] = ()
) -> np.ndarray:
    """Mix frames into a whole recording, starting at sample 0: the message parts, then
    message over and over."""
    mod = Modulator(message, level_db)
    mod.frames.send(parts)
    return mod.process(speech)


def _baseband(audio: np.ndarray) -> np.ndarray:
    # matched filter output, peaking at each symbol's pulse peak
    x = fftconvolve(audio, LOWPASS, mode="same")
    x = x * np.exp(-2j * np.pi * CARRIER * np.arange(len(x)) / RATE)
    return fftconvolve(x, PULSE, mode="same")


def _sync_scores(y: np.ndarray) -> np.ndarray:
    # normalized correlation of the sync word with the phase changes between symbols, for a
    # first sync symbol peaking at each sample from SYMBOL on
    d = y[SYMBOL:] * np.conj(y[:-SYMBOL])
    taps = np.zeros((len(SYNC) - 1) * SYMBOL + 1)
    taps[::SYMBOL] = SYNC
    s = correlate(d.real, taps, mode="valid", method="fft")
    power = correlate(np.abs(d) ** 2, np.abs(taps), mode="valid", method="fft")
    return s / np.sqrt(len(SYNC) * np.maximum(power, 1e-30))


def _timing(y: np.ndarray, t0: int) -> tuple[float, float]:
    # offset of symbol 0 from t0 and the clock drift per symbol, from the symbol rate line in
    # |y|^2 (Oerder and Meyr) in each block of the frame, fitted with a line
    e = np.abs(y[t0 - SYMBOL // 2 : t0 - SYMBOL // 2 + FRAME]) ** 2
    ref = np.exp(-2j * np.pi * (np.arange(TIMING_BLOCK * SYMBOL) - SYMBOL // 2) / SYMBOL)
    line = e.reshape(-1, TIMING_BLOCK * SYMBOL) @ ref
    offsets = -SYMBOL / (2 * np.pi) * np.unwrap(np.angle(line))
    centers = TIMING_BLOCK * np.arange(len(line)) + TIMING_BLOCK // 2
    drift, offset = np.polyfit(centers, offsets, 1, w=np.abs(line))
    return float(offset), float(drift)


def _frame(y: np.ndarray, t0: int) -> Frame | None:
    offset, drift = _timing(y, t0)
    at = np.round(t0 + offset + np.arange(FRAME_SYMBOLS) * (SYMBOL + drift)).astype(int)
    # a fit to noise can land anywhere
    z = y[np.clip(at, 0, len(y) - 1)]
    p = z[1:] * np.conj(z[:-1])
    # the carrier phase cancels in p, leaving the signal in the real part and only noise in
    # the imaginary part
    noise = uniform_filter1d(p.imag**2, 25, mode="nearest") + 1e-30
    payload = decode_bits((p.real / noise)[len(SYNC) - 1 :])
    message = unpack(payload)
    if message is None:
        return None
    signal = float(np.mean(_chips(payload)[1:] * p.real))
    snr = 10 * np.log10(signal**2 / (float(np.mean(p.imag**2)) + 1e-30))
    return Frame(t0 + offset - DELAY, message, snr)


def decode(audio: np.ndarray) -> list[Frame]:
    """Every frame that decodes cleanly in audio (mono, RATE), in order of start time."""
    if len(audio) < FRAME:
        return []
    y = _baseband(audio)
    scores = _sync_scores(y)
    peaks, _ = find_peaks(scores, height=SYNC_DETECT, distance=SYMBOL)
    # only frames that are in the audio to a symbol past their last one
    peaks = peaks[peaks + SYMBOL + FRAME <= len(y)]
    frames: list[Frame] = []
    for j in peaks[np.argsort(-scores[peaks])]:
        t0 = int(j) + SYMBOL
        # one frame can raise several peaks, and real frames are a whole frame apart
        if any(abs(t0 - DELAY - f.start) < FRAME / 2 for f in frames):
            continue
        frame = _frame(y, t0)
        if frame is not None:
            frames.append(frame)
    return sorted(frames, key=lambda f: f.start)
