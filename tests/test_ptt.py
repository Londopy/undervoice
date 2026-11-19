import sys
import types

import numpy as np
import pytest

from undervoice import Message
from undervoice._live import _Sender
from undervoice._ptt import Ptt, SetupError

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

    def write(self, data):
        self.log.append(("write", list(data)))

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
    with Ptt() as ptt:
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
        with Ptt(gpio=1) as ptt:
            ptt.key(True)
            raise KeyboardInterrupt
    assert hid.log[-2:] == [("write", [0, 0, 1, 0, 0]), ("close",)]


def test_needs_a_choice_between_two_interfaces(hid, monkeypatch):
    second = dict(CM108, path=b"aioc", vendor_id=0x1209, product_id=0x7388)
    monkeypatch.setattr(hid, "enumerate", lambda: [CM108, second])
    with pytest.raises(SetupError, match="pick one"):
        Ptt()
    Ptt(1)
    assert hid.log[0] == ("open", b"aioc")


def test_no_interface(hid, monkeypatch):
    monkeypatch.setattr(hid, "enumerate", lambda: [MOUSE])
    with pytest.raises(SetupError, match="no CM108"):
        Ptt()


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
