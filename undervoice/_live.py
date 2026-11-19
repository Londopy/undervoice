"""Sound card transmit and receive through sounddevice."""

from __future__ import annotations

import queue
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import ModuleType

import numpy as np
from scipy.signal import resample_poly

from ._audio import Decimator, Interpolator, to_int16
from ._demodulator import decode
from ._message import Message
from ._modulator import Modulator, encode
from ._ptt import Ptt, describe, find
from ._waveform import FRAME, RATE

CARD_RATE = 6 * RATE
BLOCK = 960
BUFFER = FRAME + 5 * RATE
EVERY = 2 * RATE
CLIP_WARN_EVERY = 30.0

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
    try:
        found = find()
    except ImportError as e:
        print(f"\nPTT interfaces: unknown ({e})")
        return
    print("\nPTT interfaces (for --ptt N):")
    for i, d in enumerate(found):
        print(f"  {i}: {describe(d)}")
    if not found:
        print("  none found")


class _Sender:
    """Mixes data into the mic audio while keyed and sends silence while not."""

    def __init__(self, message: Message, level_db: float, shape: float, delay: float):
        self.args = (message, level_db, shape)
        self.mod = Modulator(*self.args)
        self.down, self.up = Decimator(), Interpolator()
        self.delay = delay
        self.keyed = False
        self.hold = 0
        self.problems: queue.Queue[str] = queue.Queue()

    def callback(self, indata, outdata, frames, when, status) -> None:
        if status:
            self.problems.put(str(status))
        speech = self.down(indata[:, 0])
        if self.keyed and self.hold <= 0:
            outdata[:, 0] = self.up(self.mod.process(speech))
        else:
            self.hold -= len(speech)
            outdata[:, 0] = self.up(np.zeros(len(speech)))

    def key(self) -> None:
        # a fresh modulator starts a frame with each transmission, after the radio's TX delay
        self.mod = Modulator(*self.args)
        self.hold = int(self.delay * RATE)
        self.keyed = True

    def unkey(self) -> None:
        self.keyed = False


def _enter_presses() -> queue.Queue[str]:
    presses: queue.Queue[str] = queue.Queue()

    def read() -> None:
        for line in sys.stdin:
            presses.put(line)

    threading.Thread(target=read, daemon=True).start()
    return presses


def transmit(
    message: Message,
    level_db: float,
    shape: float,
    src: Device,
    dst: Device,
    ptt: Ptt | None = None,
    txdelay: float = 0.3,
) -> None:
    """Mic in, speech with data out, until Ctrl+C. With ptt, Enter keys and unkeys the radio."""
    sd = _sd()
    sender = _Sender(message, level_db, shape, txdelay if ptt else 0.0)
    if ptt is None:
        sender.key()
    with sd.Stream(CARD_RATE, BLOCK, (src, dst), channels=1, callback=sender.callback) as stream:
        if ptt is None:
            print(f"sending {message} under the mic audio, Ctrl+C to stop")
        else:
            print(f"press Enter to key {ptt.name}, Enter again to unkey, Ctrl+C to stop")
            presses = _enter_presses()
        while stream.active:
            _report(sender.problems)
            if ptt is None:
                sd.sleep(500)
                continue
            try:
                presses.get(timeout=0.5)
            except queue.Empty:
                continue
            if sender.keyed:
                sender.unkey()
                ptt.key(False)
                print("receiving")
            else:
                ptt.key(True)
                sender.key()
                print(f"transmitting {message}")
    sys.exit("audio stream stopped")


def _report(problems: queue.Queue[str]) -> None:
    while not problems.empty():
        print(f"sound card: {problems.get()}", file=sys.stderr)


def play(
    speech: np.ndarray,
    message: Message,
    level_db: float,
    shape: float,
    dst: Device,
    ptt: Ptt | None = None,
    txdelay: float = 0.3,
) -> None:
    """Play a recording with data mixed in, keying the radio around it when ptt is given."""
    sd = _sd()
    audio = resample_poly(encode(speech, message, level_db, shape), 6, 1)
    if ptt:
        # silence while the radio comes up to power, and a little after so the sound card
        # has drained before unkeying
        audio = np.concatenate(
            [np.zeros(int(txdelay * CARD_RATE)), audio, np.zeros(CARD_RATE // 5)]
        )
        ptt.key(True)
    print(f"playing {len(speech) / RATE:.1f} s with {message}")
    sd.play(audio.astype(np.float32), CARD_RATE, device=dst, blocking=True)
    if ptt:
        ptt.key(False)


@contextmanager
def _recorder(path: str | None) -> Iterator[Callable[[np.ndarray], None]]:
    if path is None:
        yield lambda block: None
        return
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        yield lambda block: out.writeframes(to_int16(block).tobytes())


def receive(shape: float, src: Device, record: str | None = None) -> None:
    """Print every frame heard on the input, until Ctrl+C. With record, also save the
    received audio to a WAV file so a session can be decoded again offline."""
    sd = _sd()
    down = Decimator()
    blocks: queue.Queue[tuple[np.ndarray, float]] = queue.Queue()

    problems: queue.Queue[str] = queue.Queue()

    def callback(indata, frames, when, status) -> None:
        if status:
            problems.put(str(status))
        blocks.put((down(indata[:, 0].copy()), float(np.max(np.abs(indata)))))

    audio = np.zeros(0)
    origin = 0
    pending = 0
    heard: list[float] = []
    warned = -CLIP_WARN_EVERY
    with (
        sd.InputStream(CARD_RATE, BLOCK, src, channels=1, callback=callback) as stream,
        _recorder(record) as save,
    ):
        print("listening, Ctrl+C to stop" + (f", recording to {record}" if record else ""))
        while stream.active:
            _report(problems)
            try:
                block, peak = blocks.get(timeout=1.0)
            except queue.Empty:
                continue
            save(block)
            if peak > 0.98 and time.monotonic() - warned > CLIP_WARN_EVERY:
                warned = time.monotonic()
                print("input is clipping: turn the sound card input level down", file=sys.stderr)
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
