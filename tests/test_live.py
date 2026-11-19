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
from undervoice._live import BLOCK, Listener

SPEECH = Path(__file__).parent.parent / "speech" / "OSR_us_000_0010_8k.wav"
MSG = Message("W1AW", "FN31pr", "FAKE CARD TEST")


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


@pytest.fixture
def fake_sd(monkeypatch):
    module = types.SimpleNamespace(
        InputStream=FakeInput,
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


def test_window_enables_transmit_only_for_a_valid_station(fake_sd, monkeypatch):
    monkeypatch.setitem(sys.modules, "hid", types.SimpleNamespace(enumerate=lambda: []))
    from undervoice._gui import App

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    try:
        app = App(root)
        assert app.inputs == [(0, "Mic"), (1, "USB Audio Device")]
        assert app.to_radio.get() == "USB Audio Device"
        assert app.tx_button.instate(["disabled"])
        app.call.set("w1aw")
        app.grid_sq.set("FN31pr")
        assert not app.tx_button.instate(["disabled"])
        app.text.set("price: $5")
        assert app.tx_button.instate(["disabled"])
        assert "outside A-Z" in app.message_error.cget("text")
    finally:
        root.destroy()
