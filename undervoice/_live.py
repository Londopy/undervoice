"""Sound card transmit and receive through sounddevice."""

from __future__ import annotations

import queue
import sys
import threading
import time
import wave
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import numpy as np
from scipy.signal import resample_poly

from ._audio import Decimator, Interpolator, to_int16
from ._demodulator import Frame, decode
from ._message import Message
from ._modulator import Modulator, encode
from ._ptt import Ptt, SetupError, describe, find
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
    except ImportError as e:
        raise SetupError(
            "sound card support needs sounddevice: pip install undervoice[live]"
        ) from e
    except OSError as e:
        # sounddevice raises this when the PortAudio library itself is missing
        raise SetupError(
            f"{e}; on Debian or Raspberry Pi OS: sudo apt install libportaudio2"
        ) from e
    return sounddevice


def cards() -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """(inputs, outputs) as (index, name), for the system's default sound API only, since
    Windows lists every card once per API."""
    sd = _sd()
    api = sd.default.hostapi
    inputs, outputs = [], []
    for i, d in enumerate(sd.query_devices()):
        if d["hostapi"] != api:
            continue
        if d["max_input_channels"] > 0:
            inputs.append((i, d["name"]))
        if d["max_output_channels"] > 0:
            outputs.append((i, d["name"]))
    return inputs, outputs


def devices() -> None:
    print(_sd().query_devices())
    try:
        found = find()
    except SetupError as e:
        print(f"\nPTT interfaces: unknown ({e})")
        return
    print("\nPTT interfaces (for --ptt N):")
    for i, d in enumerate(found):
        print(f"  {i}: {describe(d)}")
    if not found:
        print("  none found")


def _open(opener: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    sd = _sd()
    try:
        return opener(*args, **kwargs)
    except (sd.PortAudioError, ValueError) as e:
        # PortAudioError: a busy card or a rate it can't do; ValueError: no such device
        raise SetupError(f"can't open the sound card: {e}") from e


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


class Transmitter:
    """Mic in, speech with data out, keyed and unkeyed on demand."""

    def __init__(
        self,
        message: Message,
        level_db: float,
        shape: float,
        src: Device,
        dst: Device,
        ptt: Ptt | None = None,
        txdelay: float = 0.3,
    ):
        self.message = message
        self.ptt = ptt
        self.sender = _Sender(message, level_db, shape, txdelay if ptt else 0.0)
        sd = _sd()
        self.stream = _open(
            sd.Stream, CARD_RATE, BLOCK, (src, dst), channels=1, callback=self.sender.callback
        )
        self.stream.start()

    @property
    def keyed(self) -> bool:
        return self.sender.keyed

    @property
    def active(self) -> bool:
        return bool(self.stream.active)

    def key(self) -> None:
        if self.ptt:
            self.ptt.key(True)
        self.sender.key()

    def unkey(self) -> None:
        self.sender.unkey()
        if self.ptt:
            self.ptt.key(False)

    def close(self) -> None:
        self.unkey()
        self.stream.stop()
        self.stream.close()


def _enter_presses() -> queue.Queue[str]:
    presses: queue.Queue[str] = queue.Queue()

    def read() -> None:
        for line in sys.stdin:
            presses.put(line)

    threading.Thread(target=read, daemon=True).start()
    return presses


def _report(problems: queue.Queue[str]) -> None:
    while not problems.empty():
        print(f"sound card: {problems.get()}", file=sys.stderr)


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
    tx = Transmitter(message, level_db, shape, src, dst, ptt, txdelay)
    try:
        if ptt is None:
            tx.key()
            print(f"sending {message} under the mic audio, Ctrl+C to stop")
        else:
            print(f"press Enter to key {ptt.name}, Enter again to unkey, Ctrl+C to stop")
            presses = _enter_presses()
        while tx.active:
            _report(tx.sender.problems)
            if ptt is None:
                time.sleep(0.5)
                continue
            try:
                presses.get(timeout=0.5)
            except queue.Empty:
                continue
            if tx.keyed:
                tx.unkey()
                print("receiving")
            else:
                tx.key()
                print(f"transmitting {message}")
    finally:
        tx.close()
    raise SetupError("audio stream stopped")


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
    try:
        _open(sd.play, audio.astype(np.float32), CARD_RATE, device=dst, blocking=True)
    finally:
        if ptt:
            ptt.key(False)


def stop_playing() -> None:
    _sd().stop()


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


@dataclass(frozen=True)
class Heard:
    when: float
    frame: Frame


class Listener:
    """Decodes frames from a sound card input on its own thread. Frames arrive on `heard`,
    warnings on `notes`, and `peak` is the latest input peak for a level meter."""

    def __init__(self, shape: float, src: Device, record: str | None = None):
        self.shape = shape
        self.heard: queue.Queue[Heard] = queue.Queue()
        self.notes: queue.Queue[str] = queue.Queue()
        self.peak = 0.0
        self._down = Decimator()
        self._blocks: queue.Queue[np.ndarray] = queue.Queue()
        self._stop = threading.Event()
        sd = _sd()
        self._stack = ExitStack()
        try:
            self._save = self._stack.enter_context(_recorder(record))
            stream = _open(sd.InputStream, CARD_RATE, BLOCK, src, channels=1, callback=self._feed)
            self._stream = self._stack.enter_context(stream)
        except OSError as e:
            self._stack.close()
            raise SetupError(f"can't record to {record}: {e}") from e
        except SetupError:
            self._stack.close()
            raise
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._stack.close()

    def _feed(self, indata, frames, when, status) -> None:
        if status:
            self.notes.put(f"sound card: {status}")
        self.peak = float(np.max(np.abs(indata)))
        self._blocks.put(self._down(indata[:, 0].copy()))

    def _run(self) -> None:
        audio = np.zeros(0)
        origin = 0
        pending = 0
        seen: list[float] = []
        warned = -CLIP_WARN_EVERY
        while not self._stop.is_set():
            if not self._stream.active and self._blocks.empty():
                self.notes.put("audio stream stopped")
                return
            try:
                block = self._blocks.get(timeout=0.5)
            except queue.Empty:
                continue
            self._save(block)
            if self.peak > 0.98 and time.monotonic() - warned > CLIP_WARN_EVERY:
                warned = time.monotonic()
                self.notes.put("input is clipping: turn the sound card input level down")
            audio = np.concatenate([audio, block])
            pending += len(block)
            if len(audio) > BUFFER:
                origin += len(audio) - BUFFER
                audio = audio[-BUFFER:]
            if pending < EVERY:
                continue
            pending = 0
            for frame in decode(audio, self.shape):
                at = origin + frame.start
                # the same frame stays in the buffer for a few passes
                if any(abs(at - s) < RATE for s in seen):
                    continue
                seen = [*seen[-20:], at]
                self.heard.put(Heard(time.time(), frame))


def receive(shape: float, src: Device, record: str | None = None) -> None:
    """Print every frame heard on the input, until Ctrl+C. With record, also save the
    received audio to a WAV file so a session can be decoded again offline."""
    listener = Listener(shape, src, record)
    print("listening, Ctrl+C to stop" + (f", recording to {record}" if record else ""))
    try:
        while listener.running:
            try:
                h = listener.heard.get(timeout=0.5)
                stamp = time.strftime("%H:%M:%SZ", time.gmtime(h.when))
                print(f"{stamp}  {h.frame.message}  (snr {h.frame.snr_db:.1f} dB)", flush=True)
            except queue.Empty:
                pass
            while not listener.notes.empty():
                print(listener.notes.get(), file=sys.stderr)
    finally:
        listener.stop()
    raise SetupError("audio stream stopped")
