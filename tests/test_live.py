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

from undervoice import FRAME, RATE, Message, encode, read_wav, write_wav
from undervoice import _live
from undervoice._cli import main
from undervoice._config import load as load_settings
from undervoice._cwid import morse
from undervoice._live import BLOCK, CARD_RATE, Listener, Transmitter, _Sender
from undervoice._ptt import Keyer, SetupError
from undervoice._subaudible import FRAME as SUB_FRAME
from undervoice._subaudible import decode as sub_decode
from undervoice._subaudible import encode as sub_encode

SPEECH = Path(__file__).parent.parent / "speech" / "OSR_us_000_0010_8k.wav"
needs_speech = pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
MSG = Message("W1AW", "FN31pr", "FAKE CARD TEST")
# window settings for tests: no update checks and no listening until a test asks
QUIET = "[updater]\nenabled = false\n\n[receive]\nalways = false\n"


class FakeInput:
    """Plays a fixed recording into the callback as fast as it is taken, then stops like a
    card that went away."""

    audio = np.zeros(0)

    def __init__(self, rate, blocksize, device, channels, callback):
        self.callback = callback
        self.active = False

    def start(self):
        self.active = True
        threading.Thread(target=self._feed, daemon=True).start()

    def stop(self):
        self.active = False

    def close(self):
        pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.active = False

    def _feed(self):
        for i in range(0, len(self.audio) - BLOCK, BLOCK):
            self.callback(self.audio[i : i + BLOCK, None], BLOCK, None, None)
        self.active = False


class FakeDuplex:
    """A duplex stream that records whether it is running and keeps its callback, for a test
    to drive with drive()."""

    def __init__(self, rate, blocksize, device, channels, callback):
        self.active = False
        self.callback = callback

    def start(self):
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        pass


class RunningDuplex(FakeDuplex):
    """Calls back on its own thread, about four times faster than real time, with `mic` as
    the input (silence when it is None) until the mic runs out, and keeps what it sent."""

    mic: np.ndarray | None = None

    def start(self):
        self.active = True
        self.sent = []
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        out = np.zeros((BLOCK, 1))
        i = 0
        while self.active:
            if self.mic is None:
                block = np.zeros((BLOCK, 1))
            elif i + BLOCK <= len(self.mic):
                block = self.mic[i : i + BLOCK, None]
            else:
                self.active = False
                break
            self.callback(block, out, BLOCK, None, None)
            self.sent.append(out[:, 0].copy())
            i += BLOCK
            time.sleep(0.005)


def drive(callback, mic):
    """Run 48 kHz mic audio through a duplex stream callback block by block; what it sent."""
    out = np.zeros((BLOCK, 1))
    sent = []
    for i in range(0, len(mic) - BLOCK + 1, BLOCK):
        callback(mic[i : i + BLOCK, None], out, BLOCK, None, None)
        sent.append(out[:, 0].copy())
    return np.concatenate(sent)


class FakeKeyer(Keyer):
    """Logs each keying, and its closing, to a list a test can share with other fakes. Keying
    the ways in `refuse` fails like rigctld refusing."""

    name = "fake PTT"

    def __init__(self, log=None, refuse=()):
        self.log = [] if log is None else log
        self.refuse = refuse

    def key(self, on):
        self.log.append(on)
        if on in self.refuse:
            raise SetupError("rigctld refused PTT: RPRT -1")

    def close(self):
        try:
            self.key(False)
        finally:
            self.log.append("closed")


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
        played=[],
        stop=lambda: None,
    )
    module.play = lambda audio, rate, device=None, blocking=False: module.played.append(
        (audio, rate, device, blocking)
    )
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return module


@pytest.fixture
def running_card(fake_sd, monkeypatch):
    """Duplex streams that run by themselves; the list of them as they open."""
    made = []

    class Card(RunningDuplex):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(fake_sd, "Stream", Card)
    return made


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


def listen(mode):
    """Every message a Listener in mode hears in FakeInput.audio."""
    listener = Listener(0.5, None, None, mode)
    deadline = time.monotonic() + 60
    while listener.running and time.monotonic() < deadline:
        time.sleep(0.05)
    listener.stop()
    heard = []
    while not listener.heard.empty():
        heard.append(listener.heard.get().frame.message)
    return heard


@needs_speech
# 22 s holds two frames under the voice and three sub-audible ones, each inside a decode pass
@pytest.mark.parametrize("mode, frames", [("voice", 2), ("sub", 3)])
def test_what_the_sender_sends_a_listener_in_the_same_mode_decodes(
    fake_sd, monkeypatch, mode, frames
):
    speech = 0.5 * read_wav(SPEECH)[: 22 * RATE]
    sender = _Sender(MSG, -18.0, 0.5, 0.0, None, mode)
    sender.key()
    monkeypatch.setattr(FakeInput, "audio", drive(sender.callback, resample_poly(speech, 6, 1)))
    assert listen(mode) == [MSG] * frames


def test_the_whole_cw_id_goes_out_before_ptt_drops(running_card):
    sent_at_unkey = []

    class Watching(FakeKeyer):
        def key(self, on):
            super().key(on)
            if not on:
                sent_at_unkey.append(len(card.sent))

    keyer = Watching()
    tx = Transmitter(Message("E"), -18.0, 0.5, None, None, keyer, 0.0)
    [card] = running_card
    tx.key()
    assert tx.close(identify=True)
    assert keyer.log == [True, False]
    assert not tx.active
    # the mic is silent, so all that went out before PTT dropped is the Morse, all of it
    before = np.concatenate(card.sent[: sent_at_unkey[0]])[::6]
    assert np.sum(before**2) == pytest.approx(np.sum(morse("E") ** 2), rel=0.05)


def test_ptt_drops_even_when_the_id_is_interrupted(fake_sd, monkeypatch):
    def interrupted(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(_live, "time", types.SimpleNamespace(sleep=interrupted))
    keyer = FakeKeyer()
    tx = Transmitter(MSG, -18.0, 0.5, None, None, keyer, 0.0)
    tx.key()
    with pytest.raises(KeyboardInterrupt):
        tx.close(identify=True)
    assert keyer.log == [True, False]
    assert not tx.active


def test_tx_with_ptt_ids_after_the_first_over_then_once_9_minutes_have_passed(
    fake_sd, monkeypatch, capsys
):
    # seven Enter presses: four overs, each keyed for five minutes
    presses = queue.Queue()
    for _ in range(7):
        presses.put("\n")
    monkeypatch.setattr(_live, "_enter_presses", lambda: presses)
    clock = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    class Card(FakeDuplex):
        # running for as long as there are presses left, which the last over's end uses up
        active = property(lambda self: not presses.empty(), lambda self, value: None)

    class FiveMinuteOvers(FakeKeyer):
        def key(self, on):
            super().key(on)
            if on:
                clock[0] += 300

    monkeypatch.setattr(fake_sd, "Stream", Card)
    keyer = FiveMinuteOvers()
    with pytest.raises(SetupError, match="audio stream stopped"):
        _live.transmit(Message("E"), -18.0, 0.5, None, None, keyer, 0.0, cw_id=True)
    # after the first over, and after the third, 10 minutes after the first ID
    assert capsys.readouterr().out.count("sending CW ID") == 2
    assert keyer.log == [True, False] * 4


def test_no_id_once_the_sound_card_has_stopped(fake_sd):
    keyer = FakeKeyer()
    tx = Transmitter(MSG, -18.0, 0.5, None, None, keyer, 0.0)
    tx.key()
    tx.stream.active = False
    assert not tx.close(identify=True)
    assert len(tx.sender.override) == 0
    assert keyer.log == [True, False]


@needs_speech
def test_tx_play_keys_sends_sub_audible_data_ends_with_the_id_and_unkeys(
    fake_sd, monkeypatch, tmp_path
):
    log, played = [], []
    monkeypatch.setattr("undervoice._cli.open_keyer", lambda how, gpio: FakeKeyer(log))

    def play(audio, rate, device, blocking):
        log.append("play")
        played.append((audio, rate, device, blocking))

    monkeypatch.setattr(fake_sd, "play", play)
    wav = tmp_path / "over.wav"
    speech = read_wav(SPEECH)[: 12 * RATE]
    write_wav(wav, speech)
    args = ["--call", "W1AW", "--output", "Radio", "--ptt", "fake", "--txdelay", "0.5"]
    assert main(["tx", "--play", str(wav), *args, "--mode", "sub", "--cw-id"]) == 0
    # keyed for the whole recording, then unkeyed and let go
    assert log == [True, "play", False, False, "closed"]
    [(audio, rate, device, blocking)] = played
    assert (rate, device, blocking) == (CARD_RATE, "Radio", True)
    x = resample_poly(audio, 1, 6)[RATE // 2 :]
    assert [f.message for f in sub_decode(x[: len(speech)])] == [Message("W1AW")] * (
        len(speech) // SUB_FRAME
    )
    # then a quarter second, the Morse, and 0.2 s for the card to drain before unkeying
    tone = morse("W1AW")
    start = len(speech) + RATE // 4
    np.testing.assert_allclose(x[start : start + len(tone)], tone, atol=0.01)
    assert len(x) - start - len(tone) == RATE // 5


def test_tx_play_unkeys_when_the_sound_card_fails(fake_sd, monkeypatch, tmp_path):
    log = []
    monkeypatch.setattr("undervoice._cli.open_keyer", lambda how, gpio: FakeKeyer(log))

    def busy(audio, rate, device, blocking):
        log.append("play")
        raise fake_sd.PortAudioError("device busy")

    monkeypatch.setattr(fake_sd, "play", busy)
    wav = tmp_path / "over.wav"
    write_wav(wav, np.zeros(RATE))
    with pytest.raises(SystemExit, match="device busy"):
        main(["tx", "--play", str(wav), "--call", "W1AW", "--ptt", "fake"])
    assert log == [True, "play", False, False, "closed"]


def test_tx_from_the_mic_in_sub_mode_sends_sub_audible_data(running_card, monkeypatch):
    monkeypatch.setattr(RunningDuplex, "mic", np.zeros(7 * CARD_RATE))
    with pytest.raises(SystemExit, match="audio stream stopped"):
        main(["tx", "--call", "W1AW", "--mode", "sub"])
    [card] = running_card
    sent = resample_poly(np.concatenate(card.sent), 1, 6)
    assert [f.message for f in sub_decode(sent)] == [Message("W1AW")]


def test_rx_in_sub_mode_prints_sub_audible_frames(fake_sd, monkeypatch, capsys):
    audio = resample_poly(sub_encode(np.zeros(7 * RATE), MSG), 6, 1)
    monkeypatch.setattr(FakeInput, "audio", audio)
    with pytest.raises(SystemExit, match="audio stream stopped"):
        main(["rx", "--mode", "sub"])
    assert capsys.readouterr().out.count(str(MSG)) == 1


@needs_speech
def test_check_in_sub_mode_plays_back_sub_audible_data(fake_sd, monkeypatch, capsys):
    monkeypatch.setattr(FakeInput, "audio", resample_poly(read_wav(SPEECH)[: 12 * RATE], 6, 1))
    assert main(["check", "--call", "W1AW", "--mode", "sub"]) == 0
    assert "the data mixed into it decodes: W1AW" in capsys.readouterr().out
    [(audio, rate, device, blocking)] = fake_sd.played
    assert blocking
    assert [f.message for f in sub_decode(resample_poly(audio, 1, 6))] == [Message("W1AW")]


def test_check_reports_a_microphone_that_stops(fake_sd, monkeypatch):
    monkeypatch.setattr(FakeInput, "audio", np.full(3 * CARD_RATE, 0.1))
    with pytest.raises(SystemExit, match="the microphone stopped after 3.0 s"):
        main(["check", "--call", "W1AW"])
    assert fake_sd.played == []


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
    def __init__(self, shape, src, record, mode):
        self.src = src
        self.mode = mode
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
    assert app.listener.mode == "voice"


def test_cw_id_goes_out_before_unkeying_then_waits_ten_minutes(window):
    make, _ = window
    app = make()
    app.call.set("W1AW")
    app.cw_id.set(True)
    app.toggle_tx()
    tx = app.tx
    app.stop_sending()
    assert app.identifying
    # the ID is queued from a background thread so the window stays responsive
    deadline = time.monotonic() + 2
    while not len(tx.sender.override) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(tx.sender.override) == len(morse("W1AW"))
    deadline = time.monotonic() + 10
    while app.identifying and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.05)
    assert not app.identifying
    assert not tx.active
    # a second over straight after does not identify again
    app.toggle_tx()
    second = app.tx
    app.stop_sending()
    assert not app.identifying
    assert len(second.sender.override) == 0


def test_reminder_to_identify_without_cw_id(window):
    make, _ = window
    app = make()
    app.last_over = time.monotonic()
    app.last_id = time.monotonic() - 601
    app._remind_id()
    assert "callsign" in app.status.cget("text")


def test_mode_choice_is_remembered(window):
    from undervoice._gui import MODE_NAMES

    make, path = window
    app = make()
    app.mode.set(MODE_NAMES["sub"])
    app.close()
    assert load_settings(path)["radio"]["mode"] == "sub"


def fake_ptt(app, monkeypatch, refuse=()):
    """Has the window key the radio through FakeKeyers; the list of them as they open."""
    import undervoice._gui as gui

    made = []

    def open_fake(how):
        made.append(FakeKeyer(refuse=refuse))
        return made[-1]

    monkeypatch.setattr(gui, "open_keyer", open_fake)
    app.keyers.append(("Fake PTT", "fake"))
    app.ptt_box.set("Fake PTT")
    return made


def wait_for(app, done, seconds=10.0):
    """Run the window until done(), for about seconds at most; then done(). It counts turns
    rather than reading the clock, so a test can fake time.monotonic."""
    for _ in range(int(seconds / 0.02)):
        if done():
            break
        app.root.update()
        time.sleep(0.02)
    return done()


def test_closing_the_window_during_the_cw_id_waits_for_it_to_unkey(window, monkeypatch):
    make, _ = window
    app = make()
    keyers = fake_ptt(app, monkeypatch)
    app.call.set("E")
    app.cw_id.set(True)
    app.toggle_tx()
    app.stop_sending()
    assert app.identifying
    app.close()
    # unkeyed after the ID, then once more as the keyer is let go
    assert keyers[0].log == [True, False, False, "closed"]


def test_the_ptt_test_holds_the_radio_even_through_closing_the_window(window, monkeypatch):
    make, _ = window
    app = make()
    keyers = fake_ptt(app, monkeypatch)
    app.call.set("W1AW")
    app.test_ptt()
    # no second keyer, which the test would then unkey under it
    app.toggle_tx()
    app._space_down(None)
    assert app.tx is None
    assert len(keyers) == 1
    app.close()
    assert keyers[0].log == [True, False, False, "closed"]


def test_a_callsign_with_a_space_goes_out_in_morse(window, monkeypatch):
    make, _ = window
    app = make()
    keyers = fake_ptt(app, monkeypatch)
    app.call.set("E E")
    app.cw_id.set(True)
    app.toggle_tx()
    tx = app.tx
    app.stop_sending()
    assert wait_for(app, lambda: not app.identifying)
    assert len(tx.sender.override) == len(morse("E E"))
    assert app.status.cget("text") == "Sent your callsign in Morse and unkeyed."
    assert not app.tx_button.instate(["disabled"])
    assert keyers[0].log[-1] == "closed"


def test_unkeying_that_fails_after_the_cw_id_is_reported_and_frees_the_window(window, monkeypatch):
    make, _ = window
    app = make()
    keyers = fake_ptt(app, monkeypatch, refuse={False})
    app.call.set("E")
    app.cw_id.set(True)
    app.toggle_tx()
    app.stop_sending()
    assert wait_for(app, lambda: not app.identifying)
    assert app.status.cget("text") == "Could not unkey the radio: rigctld refused PTT: RPRT -1"
    assert keyers[0].log[-1] == "closed"
    assert not app.tx_button.instate(["disabled"])
    # nothing says the ID went out, so the next over sends it again
    app.toggle_tx()
    app.stop_sending()
    assert app.identifying
    assert wait_for(app, lambda: not app.identifying)


def test_space_and_test_ptt_do_nothing_while_the_cw_id_goes_out(window, monkeypatch):
    make, _ = window
    app = make()
    keyers = fake_ptt(app, monkeypatch)
    app.call.set("E")
    app.cw_id.set(True)
    app._space_down(None)
    app._space_up(None)
    assert wait_for(app, lambda: app.identifying)
    app._space_down(None)
    app.test_ptt()
    assert app.tx is None
    assert len(keyers) == 1
    assert wait_for(app, lambda: not app.identifying)
    assert keyers[0].log[-1] == "closed"


def test_changing_the_mode_listens_again_in_the_new_one(window, monkeypatch):
    import undervoice._gui as gui

    make, _ = window
    monkeypatch.setattr(gui, "Listener", IdleListener)
    app = make()
    app.toggle_rx()
    first = app.listener
    app.mode.set(gui.MODE_NAMES["sub"])
    # Tk on macOS queues the event instead of handling it at once
    app.mode.event_generate("<<ComboboxSelected>>")
    assert wait_for(app, lambda: not first.running)
    assert app.listener.mode == "sub"


def test_a_ptt_test_that_fails_is_reported_and_can_be_tried_again(window, monkeypatch):
    make, _ = window
    app = make()
    keyers = fake_ptt(app, monkeypatch, refuse={True, False})
    app.test_ptt()
    assert app.ptt_button.instate(["disabled"])
    assert wait_for(app, lambda: not app.testing_ptt)
    assert app.status.cget("text") == "PTT test failed: rigctld refused PTT: RPRT -1"
    assert not app.ptt_button.instate(["disabled"])
    assert keyers[0].log[-1] == "closed"


def test_a_radio_that_will_not_key_leaves_nothing_open(window, monkeypatch, fake_sd):
    streams = []

    class Card(FakeDuplex):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            streams.append(self)

    monkeypatch.setattr(fake_sd, "Stream", Card)
    make, _ = window
    app = make()
    keyers = fake_ptt(app, monkeypatch, refuse={True, False})
    app.call.set("W1AW")
    app.toggle_tx()
    assert app.tx is None and app.ptt is None
    assert [s.active for s in streams] == [False]
    assert keyers[0].log[-1] == "closed"
    assert "rigctld refused PTT" in app.status.cget("text")
    assert app.state.cget("text") == "Idle"
    assert app.tx_button.cget("text") == "Transmit"


def test_in_pauses_above_the_data_level_is_refused(window):
    from undervoice._gui import MODE_NAMES

    make, _ = window
    app = make()
    app.call.set("W1AW")
    app.level.set("-36")
    app.pause.set("-30")
    app.toggle_tx()
    assert app.tx is None
    assert app.status.cget("text") == "In pauses (-30 dB) can't be above the data level (-36 dB)."
    # sub-audible data has no pauses to fill
    app.mode.set(MODE_NAMES["sub"])
    app.toggle_tx()
    assert app.tx is not None


def test_transmit_in_sub_mode_sends_sub_audible_data(window):
    from undervoice._gui import MODE_NAMES

    make, _ = window
    app = make()
    app.call.set("W1AW")
    app.mode.set(MODE_NAMES["sub"])
    app.toggle_tx()
    sent = drive(app.tx.stream.callback, np.zeros(7 * CARD_RATE))
    assert [f.message for f in sub_decode(resample_poly(sent, 1, 6))] == [Message("W1AW")]


def recording(monkeypatch, tmp_path, speech):
    """Has Send recording pick a WAV of speech."""
    import undervoice._gui as gui

    wav = tmp_path / "over.wav"
    write_wav(wav, speech)
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **kwargs: str(wav))


@needs_speech
def test_a_recording_goes_out_sub_audible_inside_ptt_and_ends_with_the_id(
    window, monkeypatch, fake_sd, tmp_path
):
    from undervoice._gui import MODE_NAMES

    speech = read_wav(SPEECH)[: 12 * RATE]
    recording(monkeypatch, tmp_path, speech)
    make, _ = window
    app = make()
    keyers = fake_ptt(app, monkeypatch)
    app.call.set("W1AW")
    app.cw_id.set(True)
    app.mode.set(MODE_NAMES["sub"])
    app.toggle_recording()
    assert wait_for(app, lambda: not app.sending)
    [(audio, rate, device, blocking)] = fake_sd.played
    # after the 0.3 s TX delay; it ends with the Morse and 0.2 s for the card to drain
    x = resample_poly(audio, 1, 6)[int(0.3 * RATE) :]
    assert [f.message for f in sub_decode(x[: len(speech)])] == [Message("W1AW")] * 2
    tone = morse("W1AW")
    np.testing.assert_allclose(x[-RATE // 5 - len(tone) : -RATE // 5], tone, atol=0.01)
    assert keyers[0].log == [True, False, False, "closed"]
    # the ID went out, so an over straight after does not send another
    app.toggle_tx()
    app.stop_sending()
    assert not app.identifying


def test_a_recording_that_never_played_leaves_the_id_due(window, monkeypatch, fake_sd, tmp_path):
    def busy(audio, rate, device, blocking):
        raise fake_sd.PortAudioError("device busy")

    monkeypatch.setattr(fake_sd, "play", busy)
    recording(monkeypatch, tmp_path, np.zeros(RATE))
    make, _ = window
    app = make()
    keyers = fake_ptt(app, monkeypatch)
    app.call.set("E")
    app.cw_id.set(True)
    app.toggle_recording()
    assert wait_for(app, lambda: not app.sending)
    assert app.status.cget("text") == "can't open the sound card: device busy"
    assert keyers[0].log[-1] == "closed"
    # so the next over ends with it
    app.toggle_tx()
    app.stop_sending()
    assert app.identifying
    assert wait_for(app, lambda: not app.identifying)


def test_a_sound_card_that_stops_mid_over_is_not_covered_up(window, monkeypatch):
    make, _ = window
    app = make()
    keyers = fake_ptt(app, monkeypatch)
    app.call.set("E")
    app.cw_id.set(True)
    app.toggle_tx()
    app.tx.stream.active = False
    assert wait_for(app, lambda: keyers[0].log[-1:] == ["closed"] and not app.identifying)
    assert app.status.cget("text") == "The sound card stopped while transmitting."
    # no Morse could go out, so the next over still ends with it
    app.toggle_tx()
    app.stop_sending()
    assert app.identifying
    assert wait_for(app, lambda: not app.identifying)


def fake_clock(monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    return clock


def test_a_long_over_is_reminded_to_identify_at_nine_minutes_even_with_cw_id(window, monkeypatch):
    clock = fake_clock(monkeypatch)
    make, _ = window
    app = make()
    app.call.set("W1AW")
    app.cw_id.set(True)
    app.toggle_tx()
    before = app.status.cget("text")
    app._remind_id()
    clock[0] += 8.5 * 60
    app._remind_id()
    assert app.status.cget("text") == before
    clock[0] += 0.5 * 60
    app._remind_id()
    assert app.status.cget("text") == (
        "Time to identify: say your callsign, or stop to send the CW ID."
    )


def test_with_cw_id_a_pause_in_the_qso_gets_no_reminder_but_a_late_over_does(window, monkeypatch):
    clock = fake_clock(monkeypatch)
    make, _ = window
    app = make()
    app.call.set("E")
    app.cw_id.set(True)
    app.toggle_tx()
    app.stop_sending()
    assert wait_for(app, lambda: not app.identifying)
    sent = app.status.cget("text")
    # the CW ID goes out when the next over ends, so there is nothing to say yet
    clock[0] += 9.5 * 60
    app._remind_id()
    assert app.status.cget("text") == sent
    app.toggle_tx()
    app._remind_id()
    assert app.status.cget("text") == (
        "Time to identify: say your callsign, or stop to send the CW ID."
    )


def test_without_cw_id_the_reminder_runs_through_the_qso_and_stops_after_it(window, monkeypatch):
    clock = fake_clock(monkeypatch)
    make, _ = window
    app = make()
    app.call.set("W1AW")
    app.toggle_tx()
    app._remind_id()
    app.stop_sending()
    clock[0] += 9 * 60
    app._remind_id()
    assert app.status.cget("text") == "Time to identify: say your callsign."
    app.say("")
    # once the station has been quiet for 10 minutes the QSO is taken to be over
    clock[0] += 9 * 60
    app._remind_id()
    assert app.status.cget("text") == ""


def test_a_sent_recording_counts_as_an_over_for_the_reminder(
    window, monkeypatch, fake_sd, tmp_path
):
    clock = fake_clock(monkeypatch)
    recording(monkeypatch, tmp_path, np.zeros(RATE))
    make, _ = window
    app = make()
    app.call.set("W1AW")
    app.toggle_recording()
    app._remind_id()
    assert wait_for(app, lambda: not app.sending)
    clock[0] += 9 * 60
    app._remind_id()
    assert app.status.cget("text") == "Time to identify: say your callsign."


@needs_speech
def test_the_mic_check_plays_back_what_sub_audible_mode_would_send(window, monkeypatch, fake_sd):
    from undervoice._gui import MODE_NAMES

    monkeypatch.setattr(FakeInput, "audio", resample_poly(read_wav(SPEECH)[: 12 * RATE], 6, 1))
    make, _ = window
    app = make()
    app.call.set("W1AW")
    app.mode.set(MODE_NAMES["sub"])
    app.test_microphone()
    # the microphone is busy until the check has its recording
    assert app.tx_button.instate(["disabled"])
    assert wait_for(app, lambda: fake_sd.played, 20)
    [(audio, rate, device, blocking)] = fake_sd.played
    assert [f.message for f in sub_decode(resample_poly(audio, 1, 6))] == [Message("W1AW")]
    assert "the data decoded" in app.status.cget("text")
    assert not app.tx_button.instate(["disabled"])


def test_a_mic_check_cut_short_by_the_sound_card_says_so(window, monkeypatch, fake_sd):
    monkeypatch.setattr(FakeInput, "audio", np.full(3 * CARD_RATE, 0.1))
    make, _ = window
    app = make()
    app.call.set("W1AW")
    app.test_microphone()
    assert wait_for(app, lambda: app.mic_check is None)
    assert app.status.cget("text").startswith("the microphone stopped after 3.0 s")
    assert app.mic_button.cget("text") == "Test microphone"
    assert not app.mic_button.instate(["disabled"])
    assert fake_sd.played == []
