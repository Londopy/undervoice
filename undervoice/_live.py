"""Sound card transmit and receive through sounddevice."""

from __future__ import annotations

import queue
import sys
import time
from types import ModuleType

import numpy as np
from scipy.signal import resample_poly

from ._audio import Decimator, Interpolator
from ._demodulator import decode
from ._message import Message
from ._modulator import Modulator, encode
from ._waveform import FRAME, RATE

CARD_RATE = 6 * RATE
BLOCK = 960
BUFFER = FRAME + 5 * RATE
EVERY = 2 * RATE

Device = int | str | None


def _sd() -> ModuleType:
    try:
        import sounddevice
    except ImportError:
        sys.exit("sound card support needs sounddevice: pip install undervoice[live]")
    except OSError as e:
        # sounddevice raises this when the PortAudio library itself is missing
        sys.exit(f"{e}; on Debian or Raspberry Pi OS: sudo apt install libportaudio2")
    return sounddevice


def devices() -> None:
    print(_sd().query_devices())


def transmit(message: Message, level_db: float, shape: float, src: Device, dst: Device) -> None:
    """Mic in, speech with data out, until Ctrl+C."""
    sd = _sd()
    mod = Modulator(message, level_db, shape)
    down, up = Decimator(), Interpolator()

    problems: queue.Queue[str] = queue.Queue()

    def callback(indata, outdata, frames, when, status) -> None:
        if status:
            problems.put(str(status))
        outdata[:, 0] = up(mod.process(down(indata[:, 0])))

    with sd.Stream(CARD_RATE, BLOCK, (src, dst), channels=1, callback=callback) as stream:
        print(f"sending {message} under the mic audio, Ctrl+C to stop")
        while stream.active:
            sd.sleep(500)
            _report(problems)
    sys.exit("audio stream stopped")


def _report(problems: queue.Queue[str]) -> None:
    while not problems.empty():
        print(f"sound card: {problems.get()}", file=sys.stderr)


def play(speech: np.ndarray, message: Message, level_db: float, shape: float, dst: Device) -> None:
    """Play a recording with data mixed in."""
    sd = _sd()
    audio = resample_poly(encode(speech, message, level_db, shape), 6, 1)
    print(f"playing {len(speech) / RATE:.1f} s with {message}")
    sd.play(audio.astype(np.float32), CARD_RATE, device=dst, blocking=True)


def receive(shape: float, src: Device) -> None:
    """Print every frame heard on the input, until Ctrl+C."""
    sd = _sd()
    down = Decimator()
    blocks: queue.Queue[np.ndarray] = queue.Queue()

    problems: queue.Queue[str] = queue.Queue()

    def callback(indata, frames, when, status) -> None:
        if status:
            problems.put(str(status))
        blocks.put(down(indata[:, 0].copy()))

    audio = np.zeros(0)
    origin = 0
    pending = 0
    heard: list[float] = []
    with sd.InputStream(CARD_RATE, BLOCK, src, channels=1, callback=callback) as stream:
        print("listening, Ctrl+C to stop")
        while stream.active:
            _report(problems)
            try:
                block = blocks.get(timeout=1.0)
            except queue.Empty:
                continue
            audio = np.concatenate([audio, block])
            pending += len(block)
            if len(audio) > BUFFER:
                origin += len(audio) - BUFFER
                audio = audio[-BUFFER:]
            if pending < EVERY:
                continue
            pending = 0
            for frame in decode(audio, shape):
                at = origin + frame.start
                # the same frame stays in the buffer for a few passes
                if any(abs(at - seen) < RATE for seen in heard):
                    continue
                heard = [*heard[-20:], at]
                stamp = time.strftime("%H:%M:%SZ", time.gmtime())
                print(f"{stamp}  {frame.message}  (snr {frame.snr_db:.1f} dB)", flush=True)
    sys.exit("audio stream stopped")
