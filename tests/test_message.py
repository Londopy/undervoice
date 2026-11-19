import pytest

from undervoice._message import Message, pack, unpack


@pytest.mark.parametrize(
    "msg",
    [
        Message("W1AW", "FN31pr", "ON THE AIR"),
        Message("VE3/W1AW/P", "AA00", "X" * 32),
        Message("K1ABC", "RR99xx", ""),
        Message("N0CALL", "", "QRV 146.52 / ALL GOOD."),
    ],
)
def test_round_trip(msg):
    payload = pack(msg)
    assert len(payload) == 34
    assert unpack(payload) == msg


def test_normalizes_case_and_spacing():
    msg = Message(" w1aw ", "fn31PR", "on the air ")
    assert (msg.call, msg.grid, msg.status) == ("W1AW", "FN31pr", "ON THE AIR")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"call": ""},
        {"call": "TOOLONGCALL1"},
        {"call": "W1AW", "status": "X" * 33},
        {"call": "W1AW", "status": "price: $5"},
        {"call": "W1AW", "grid": "ZZ00"},
        {"call": "W1AW", "grid": "FN3"},
    ],
)
def test_rejects_bad_fields(kwargs):
    with pytest.raises(ValueError):
        Message(**kwargs)


def test_corrupt_payload_is_rejected():
    payload = bytearray(pack(Message("W1AW", "FN31pr", "ON THE AIR")))
    payload[5] ^= 0x10
    assert unpack(bytes(payload)) is None
