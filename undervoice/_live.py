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
from ._cwid import ID_EVERY, morse
from ._demodulator import Frame
from ._message import Message
from ._modes import VOICE, decode, encode, mixer
from ._ptt import Keyer, SetupError, choices
from ._waveform import FRAME, RATE

CARD_RATE = 6 * RATE
BLOCK = 960
BUFFER = FRAME + 5 * RATE
EVERY = 2 * RATE
CLIP_WARN_EVERY = 30.0
# a pause this long (seconds) is taken as the end of an over; shorter ones are breaths and
# gaps between sentences, which the error correction rides through
NEW_OVER = 3.0
SILENCE_RATIO = 0.03
SILENCE_FLOOR = 1e-4
PAUSE_FADE = 0.999

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
    print("\nWays to key the radio (for --ptt):")
    for label, how in choices():
        print(f"  {how:<22} {label}")


def _open(opener: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    sd = _sd()
    try:
        return opener(*args, **kwargs)
    except (sd.PortAudioError, ValueError) as e:
        # PortAudioError: a busy card or a rate it can't do; ValueError: no such device
        raise SetupError(f"can't open the sound card: {e}") from e


class _Sender:
    """Mixes data into the mic audio while keyed and sends silence while not. A frame starts
    with each keying and again whenever speech comes back after a pause long enough to be a
    new over, so a radio keyed by its own PTT or VOX still gets frames lined up with overs."""

    def __init__(
        self,
        message: Message,
        level_db: float,
        shape: float,
        delay: float,
        pause_db: float | None = None,
        mode: str = VOICE,
    ):
        self.mode = mode
        self.args = (message, level_db, shape, pause_db)
        self.mod = mixer(mode, *self.args)
        self.down, self.up = Decimator(), Interpolator()
        self.delay = delay
        self.keyed = False
        self.hold = 0
        self.quiet = 0
        self.loudest = 0.0
        # audio sent in place of the mic, for the Morse ID at the end of an over
        self.override = np.zeros(0)
        self.problems: queue.Queue[str] = queue.Queue()

    def callback(self, indata, outdata, frames, when, status) -> None:
        if status:
            self.problems.put(str(status))
        speech = self.down(indata[:, 0])
        if len(self.override):
            chunk = self.override[: len(speech)]
            self.override = self.override[len(speech) :]
            outdata[:, 0] = self.up(np.pad(chunk, (0, len(speech) - len(chunk))))
        elif self.keyed and self.hold <= 0:
            # sub-audible data runs through pauses, so a restart would only waste a frame
            if self.mode == VOICE:
                self._watch_pauses(speech)
            outdata[:, 0] = self.up(self.mod.process(speech))
        else:
            self.hold -= len(speech)
            outdata[:, 0] = self.up(np.zeros(len(speech)))

    def _watch_pauses(self, speech: np.ndarray) -> None:
        level = float(np.sqrt(np.mean(speech**2)))
        # the loudest recent speech, fading by about 4 dB over ten seconds
        self.loudest = max(level, self.loudest * PAUSE_FADE)
        if level < max(SILENCE_FLOOR, SILENCE_RATIO * self.loudest):
            self.quiet += len(speech)
            return
        if self.quiet >= NEW_OVER * RATE:
            self.mod.restart()
        self.quiet = 0

    def key(self) -> None:
        # a fresh modulator starts a frame with each transmission, after the radio's TX delay
        self.mod = mixer(self.mode, *self.args)
        self.hold = int(self.delay * RATE)
        self.quiet = 0
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
        ptt: Keyer | None = None,
        txdelay: float = 0.3,
        pause_db: float | None = None,
        mode: str = VOICE,
    ):
        self.message = message
        self.ptt = ptt
        delay = txdelay if ptt else 0.0
        self.sender = _Sender(message, level_db, shape, delay, pause_db, mode)
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

    def identify(self) -> None:
        """Send the callsign in Morse, while still keyed, and wait until it has gone out."""
        tone = morse(self.message.call)
        self.sender.override = tone
        # the length of the tone plus the sound card's output buffering
        time.sleep(len(tone) / RATE + 0.3)

    def unkey(self) -> None:
        self.sender.unkey()
        if self.ptt:
            self.ptt.key(False)

    def close(self, identify: bool = False) -> None:
        """Unkey and release the sound card, sending the Morse ID first when asked."""
        if identify and self.keyed:
            self.identify()
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
    ptt: Keyer | None = None,
    txdelay: float = 0.3,
    pause_db: float | None = None,
    cw_id: bool = False,
    mode: str = VOICE,
) -> None:
    """Mic in, speech with data out, until Ctrl+C. With ptt, Enter keys and unkeys the radio.
    With cw_id, an over that ends 10 minutes or more after the last ID ends with the callsign
    in Morse."""
    tx = Transmitter(message, level_db, shape, src, dst, ptt, txdelay, pause_db, mode)
    last_id: float | None = None

    def id_due() -> bool:
        return cw_id and (last_id is None or time.monotonic() - last_id >= ID_EVERY)

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
                if id_due():
                    print("sending CW ID")
                    tx.identify()
                    last_id = time.monotonic()
                tx.unkey()
                print("receiving")
            else:
                tx.key()
                print(f"transmitting {message}")
    finally:
        tx.close(identify=id_due())
    raise SetupError("audio stream stopped")


def play(
    speech: np.ndarray,
    message: Message,
    level_db: float,
    shape: float,
    dst: Device,
    ptt: Keyer | None = None,
    txdelay: float = 0.3,
    pause_db: float | None = None,
    cw_id: bool = False,
    mode: str = VOICE,
) -> None:
    """Play a recording with data mixed in, keying the radio around it when ptt is given, and
    ending with the callsign in Morse when cw_id is set."""
    sd = _sd()
    mixed = encode(mode, speech, message, level_db, shape, pause_db)
    if cw_id:
        mixed = np.concatenate([mixed, np.zeros(RATE // 4), morse(message.call)])
    audio = resample_poly(mixed, 6, 1)
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

    def __init__(self, shape: float, src: Device, record: str | None = None, mode: str = VOICE):
        self.shape = shape
        self.mode = mode
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
            for frame in decode(self.mode, audio, self.shape):
                at = origin + frame.start
                # the same frame stays in the buffer for a few passes
                if any(abs(at - s) < RATE for s in seen):
                    continue
                seen = [*seen[-20:], at]
                self.heard.put(Heard(time.time(), frame))


def receive(shape: float, src: Device, record: str | None = None, mode: str = VOICE) -> None:
    """Print every frame heard on the input, until Ctrl+C. With record, also save the
    received audio to a WAV file so a session can be decoded again offline."""
    listener = Listener(shape, src, record, mode)
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
