import queue
import sys
import threading
import time
import tkinter as tk
import types
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import resample_poly

from undervoice import FRAME, Message, encode, read_wav
from undervoice._config import load as load_settings
from undervoice._live import BLOCK, Listener

SPEECH = Path(__file__).parent.parent / "speech" / "OSR_us_000_0010_8k.wav"
MSG = Message("W1AW", "FN31pr", "FAKE CARD TEST")
# window settings for tests: no update checks and no listening until a test asks
QUIET = "[updater]\nenabled = false\n\n[receive]\nalways = false\n"


class FakeInput:
    """Plays a fixed recording into the callback as fast as the listener takes it."""

    audio = np.zeros(0)

    def __init__(self, rate, blocksize, device, channels, callback):
        self.callback = callback
        self.active = False

    def __enter__(self):
        self.active = True
        threading.Thread(target=self._feed, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.active = False

    def _feed(self):
        for i in range(0, len(self.audio) - BLOCK, BLOCK):
            self.callback(self.audio[i : i + BLOCK, None], BLOCK, None, None)
        self.active = False


class FakeDuplex:
    """A duplex stream that only records whether it is running."""

    def __init__(self, rate, blocksize, device, channels, callback):
        self.active = False

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        pass


@pytest.fixture
def fake_sd(monkeypatch):
    module = types.SimpleNamespace(
        InputStream=FakeInput,
        Stream=FakeDuplex,
        PortAudioError=type("PortAudioError", (Exception,), {}),
        default=types.SimpleNamespace(hostapi=0),
        query_devices=lambda: [
            {"name": "Mic", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
            {
                "name": "USB Audio Device",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 2,
            },
            {
                "name": "USB Audio Device",
                "hostapi": 1,
                "max_input_channels": 1,
                "max_output_channels": 2,
            },
        ],
    )
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return module


@pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
def test_listener_reports_each_frame_once(fake_sd, tmp_path):
    speech = read_wav(SPEECH)
    FakeInput.audio = 0.5 * resample_poly(encode(speech, MSG), 6, 1)
    listener = Listener(0.5, None, str(tmp_path / "rx.wav"))
    deadline = time.monotonic() + 120
    while listener.running and time.monotonic() < deadline:
        time.sleep(0.1)
    listener.stop()
    heard = []
    while not listener.heard.empty():
        heard.append(listener.heard.get().frame.message)
    assert heard == [MSG] * (len(speech) // FRAME)
    assert listener.notes.get_nowait() == "audio stream stopped"
    assert len(read_wav(tmp_path / "rx.wav")) > 0.9 * len(speech)


@pytest.fixture(scope="session")
def tk_root():
    # one Tk per test run: Windows Tk can fail to start a second interpreter in a process
    try:
        root = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def window(fake_sd, monkeypatch, tmp_path, tk_root):
    monkeypatch.setitem(sys.modules, "hid", types.SimpleNamespace(enumerate=lambda: []))
    from undervoice._gui import App

    top = tk.Toplevel(tk_root)
    top.withdraw()
    path = tmp_path / "config.toml"
    path.write_text(QUIET, encoding="utf-8")
    yield lambda: App(top, path), path
    if top.winfo_exists():
        top.destroy()


def test_window_enables_transmit_only_for_a_valid_station(window):
    make, _ = window
    app = make()
    assert app.inputs == [(0, "Mic"), (1, "USB Audio Device")]
    assert app.to_radio.get() == "USB Audio Device"
    assert app.tx_button.instate(["disabled"])
    app.call.set("w1aw")
    app.grid_sq.set("FN31pr")
    assert not app.tx_button.instate(["disabled"])
    app.text.set("price: $5")
    assert app.tx_button.instate(["disabled"])
    assert "outside A-Z" in app.message_error.cget("text")


def test_window_remembers_settings(window):
    make, path = window
    app = make()
    app.call.set("W1AW")
    app.text.set("MOBILE")
    app.mic.set("USB Audio Device")
    app.level.set("-21")
    app.close()
    saved = load_settings(path)
    assert saved["station"]["call"] == "W1AW"
    assert saved["radio"]["mic"] == "USB Audio Device"
    assert saved["updater"]["enabled"] is False
    assert saved["receive"]["always"] is False


def test_holding_space_transmits_until_it_is_released(window):
    make, _ = window
    app = make()
    app.call.set("W1AW")
    app._space_down(None)
    first = app.tx
    assert first is not None and first.active
    # Windows repeats the key press while it is held; X11 repeats press and release
    app._space_down(None)
    app._space_up(None)
    app._space_down(None)
    assert app.tx is first
    app._space_up(None)
    time.sleep(0.15)
    app.root.update()
    assert app.tx is None
    assert not first.active


def test_space_does_nothing_without_a_valid_station(window):
    make, _ = window
    app = make()
    app._space_down(None)
    assert app.tx is None


class IdleListener:
    def __init__(self, shape, src, record):
        self.src = src
        self.running = True
        self.peak = 0.0
        self.heard = queue.Queue()
        self.notes = queue.Queue()

    def stop(self):
        self.running = False


def test_window_listens_as_soon_as_it_opens(window, monkeypatch):
    import undervoice._gui as gui

    make, path = window
    path.write_text(QUIET.replace("always = false", "always = true"), encoding="utf-8")
    monkeypatch.setattr(gui, "Listener", IdleListener)
    app = make()
    assert app.listen_always.get()
    deadline = time.monotonic() + 3
    while app.listener is None and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.05)
    assert app.listener is not None
    # the USB interface, which the window picks for audio from the radio
    assert app.listener.src == 1
    assert app.rx_button.cget("text") == "Stop listening"
