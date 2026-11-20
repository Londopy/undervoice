import socket
import sys
import threading
import types

import numpy as np
import pytest
from scipy.signal import resample_poly

from undervoice import RATE, Message
from undervoice._live import _Sender
from undervoice._modes import SUB
from undervoice._ptt import Cm108, Rigctld, SerialLine, SetupError, open_keyer
from undervoice._subaudible import decode as sub_decode

CM108 = {
    "vendor_id": 0x0D8C,
    "product_id": 0x000C,
    "path": b"cm108",
    "product_string": "USB Audio Device",
    "manufacturer_string": "C-Media",
}
MOUSE = {
    "vendor_id": 0x046D,
    "product_id": 0xC099,
    "path": b"mouse",
    "product_string": "Mouse",
    "manufacturer_string": "Logitech",
}


class FakeDevice:
    def __init__(self, log):
        self.log = log

    def open_path(self, path):
        self.log.append(("open", path))

    fails = False

    def write(self, data):
        self.log.append(("write", list(data)))
        return -1 if self.fails else len(data)

    def close(self):
        self.log.append(("close",))


@pytest.fixture
def hid(monkeypatch):
    log = []
    module = types.SimpleNamespace(enumerate=lambda: [MOUSE, CM108], device=lambda: FakeDevice(log))
    monkeypatch.setitem(sys.modules, "hid", module)
    module.log = log
    return module


def test_keys_and_unkeys_gpio3(hid):
    with Cm108() as ptt:
        ptt.key(True)
        ptt.key(False)
    assert hid.log == [
        ("open", b"cm108"),
        ("write", [0, 0, 4, 4, 0]),
        ("write", [0, 0, 4, 0, 0]),
        ("write", [0, 0, 4, 0, 0]),
        ("close",),
    ]


def test_unkeys_when_interrupted(hid):
    with pytest.raises(KeyboardInterrupt):
        with Cm108(gpio=1) as ptt:
            ptt.key(True)
            raise KeyboardInterrupt
    assert hid.log[-2:] == [("write", [0, 0, 1, 0, 0]), ("close",)]


def test_a_refused_write_is_an_error(hid, monkeypatch):
    monkeypatch.setattr(FakeDevice, "fails", True)
    ptt = Cm108()
    with pytest.raises(SetupError, match="didn't take the PTT command"):
        ptt.key(True)


def test_needs_a_choice_between_two_interfaces(hid, monkeypatch):
    second = dict(CM108, path=b"aioc", vendor_id=0x1209, product_id=0x7388)
    monkeypatch.setattr(hid, "enumerate", lambda: [CM108, second])
    with pytest.raises(SetupError, match="pick one"):
        Cm108()
    Cm108(1)
    assert hid.log[0] == ("open", b"aioc")


def test_no_interface(hid, monkeypatch):
    monkeypatch.setattr(hid, "enumerate", lambda: [MOUSE])
    with pytest.raises(SetupError, match="no CM108"):
        Cm108()


def test_interface_is_let_go_even_when_unkeying_fails(hid):
    ptt = Cm108()

    def unplugged(data):
        raise OSError("write error")

    ptt.dev.write = unplugged
    with pytest.raises(OSError, match="write error"):
        ptt.close()
    assert hid.log[-1] == ("close",)


def test_sender_waits_out_the_tx_delay_then_starts_a_frame():
    sender = _Sender(Message("W1AW"), -18.0, 0.5, delay=0.3)
    speech = np.sin(np.arange(960) / 7.0)[:, None] * 0.3
    out = np.zeros((960, 1))

    def run(blocks):
        heard = []
        for _ in range(blocks):
            sender.callback(speech, out, 960, None, None)
            heard.append(float(np.max(np.abs(out))))
        return heard

    assert max(run(5)) == 0.0
    sender.key()
    # 0.3 s at 8 kHz is 2400 samples, fifteen 160-sample blocks of silence
    assert max(run(15)) < 1e-3
    # the modulator's own output lags its input by one 160-sample hop
    assert max(run(1)) < 1e-3
    assert min(run(5)) > 0.1
    assert sender.mod.pos > 0
    sender.unkey()
    run(2)
    assert max(run(3)) < 1e-3


class FakePort:
    """Records every line change and whether the port was open at the time. Once unplugged,
    changing a line fails the way pyserial does."""

    def __init__(self):
        self.log = []
        self.is_open = False
        self.port = None
        self.unplugged = False

    def __setattr__(self, name, value):
        if name in ("rts", "dtr"):
            if self.unplugged:
                raise OSError("ClearCommError failed")
            self.log.append((name, value, self.is_open))
        object.__setattr__(self, name, value)

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False
        self.log.append(("close",))


@pytest.fixture
def serial_module(monkeypatch):
    ports = []

    def make():
        ports.append(FakePort())
        return ports[-1]

    module = types.SimpleNamespace(Serial=make, SerialException=OSError, ports=ports)
    monkeypatch.setitem(sys.modules, "serial", module)
    return module


def test_serial_lines_are_low_before_the_port_opens(serial_module):
    with open_keyer("COM5:dtr") as keyer:
        assert isinstance(keyer, SerialLine)
        keyer.key(True)
    port = serial_module.ports[0]
    assert port.port == "COM5"
    assert port.log[:2] == [("rts", False, False), ("dtr", False, False)]
    assert port.log[2:] == [("dtr", True, True), ("dtr", False, True), ("close",)]


def test_serial_defaults_to_rts(serial_module):
    keyer = open_keyer("/dev/ttyUSB0")
    keyer.key(True)
    assert serial_module.ports[0].log[-1] == ("rts", True, True)


def test_bad_serial_line_is_rejected(serial_module):
    with pytest.raises(SetupError, match="rts or dtr"):
        open_keyer("COM5:cts")


def test_serial_port_is_closed_even_when_unkeying_fails(serial_module):
    keyer = open_keyer("COM5")
    keyer.key(True)
    port = serial_module.ports[0]
    port.unplugged = True
    with pytest.raises(OSError, match="ClearCommError"):
        keyer.close()
    assert port.log[-1] == ("close",)
    assert not port.is_open


def fake_rigctld(replies):
    server = socket.create_server(("127.0.0.1", 0))
    heard = []

    def serve():
        conn, _ = server.accept()
        with conn, conn.makefile("r") as lines:
            for reply in replies:
                heard.append(lines.readline().strip())
                conn.sendall(reply.encode() + b"\n")
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1], heard


def test_rigctld_keys_by_cat_command():
    port, heard = fake_rigctld(["RPRT 0"] * 3)
    with open_keyer(f"rigctld:127.0.0.1:{port}") as keyer:
        assert isinstance(keyer, Rigctld)
        keyer.key(True)
        keyer.key(False)
    assert heard == ["T 1", "T 0", "T 0"]


def test_rigctld_refusal_is_reported():
    port, _ = fake_rigctld(["RPRT -11", "RPRT 0"])
    keyer = Rigctld("127.0.0.1", port)
    with pytest.raises(SetupError, match="RPRT -11"):
        keyer.key(True)
    keyer.close()


def test_rigctld_not_running():
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]
    with pytest.raises(SetupError, match="is it running"):
        open_keyer(f"rigctld:127.0.0.1:{port}")


def test_rigctld_is_hung_up_even_when_it_refuses_to_unkey():
    server = socket.create_server(("127.0.0.1", 0))
    hung_up = threading.Event()

    def serve():
        conn, _ = server.accept()
        with conn, conn.makefile("r") as lines:
            lines.readline()
            conn.sendall(b"RPRT -1\n")
            # a connection the other end has closed reads as an empty line
            if lines.readline() == "":
                hung_up.set()
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    keyer = Rigctld("127.0.0.1", server.getsockname()[1])
    with pytest.raises(SetupError, match="RPRT -1"):
        keyer.close()
    assert hung_up.wait(2)


def run_blocks(sender, block, count):
    """What the sender puts out at 48 kHz for count blocks of the same input."""
    out = np.zeros((960, 1))
    sent = []
    for _ in range(count):
        sender.callback(block, out, 960, None, None)
        sent.append(out[:, 0].copy())
    return np.concatenate(sent)


def test_override_goes_out_in_place_of_the_mic_then_the_mic_comes_back():
    sender = _Sender(Message("W1AW"), -18.0, 0.5, delay=0.0)
    talk = np.sin(np.arange(960) / 7.0)[:, None] * 0.3
    sender.key()
    run_blocks(sender, talk, 20)
    burst = 0.3 * np.sin(2 * np.pi * 800 * np.arange(1000) / RATE) * np.hanning(1000)
    sender.override = burst
    # back to 8 kHz; the card rate filters delay everything by 10 samples there
    sent = run_blocks(sender, talk, 12)[::6]
    assert len(sender.override) == 0
    np.testing.assert_allclose(sent[10:1010], burst, atol=0.01)
    # the burst ends 40 samples into a block, and the rest of that block is silence
    assert np.max(np.abs(sent[1010:1130])) < 0.01
    assert np.sqrt(np.mean(sent[1300:] ** 2)) > 0.1


def test_sub_audible_frames_run_straight_through_a_long_pause():
    sender = _Sender(Message("W1AW"), -18.0, 0.5, delay=0.0, mode=SUB)
    talk = np.sin(np.arange(960) / 7.0)[:, None] * 0.3
    sender.key()
    sent = [
        run_blocks(sender, talk, 50),
        run_blocks(sender, np.zeros((960, 1)), 200),
        run_blocks(sender, talk, 450),
    ]
    frames = sub_decode(resample_poly(np.concatenate(sent), 1, 6))
    # 14 s holds two frames back to back; starting afresh when the talk came back at 5 s
    # would have cut the first short and left room for only one more
    assert [f.message for f in frames] == [Message("W1AW")] * 2


def test_speech_after_a_long_pause_starts_a_new_frame():
    sender = _Sender(Message("W1AW"), -18.0, 0.5, delay=0.0)
    talk = np.sin(np.arange(960) / 7.0)[:, None] * 0.3
    hush = np.zeros((960, 1))
    sender.key()
    run_blocks(sender, talk, 50)
    run_blocks(sender, hush, 200)
    assert sender.mod.pos > 30000
    run_blocks(sender, talk, 1)
    assert sender.mod.pos <= 2 * 160


def test_a_short_pause_keeps_the_frame_going():
    sender = _Sender(Message("W1AW"), -18.0, 0.5, delay=0.0)
    talk = np.sin(np.arange(960) / 7.0)[:, None] * 0.3
    hush = np.zeros((960, 1))
    sender.key()
    run_blocks(sender, talk, 50)
    run_blocks(sender, hush, 50)
    run_blocks(sender, talk, 1)
    assert sender.mod.pos > 15000
