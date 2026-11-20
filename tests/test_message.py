import binascii

import pytest

from undervoice._message import Message, Part, normalize, pack, split, unpack


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


def test_a_long_message_splits_into_parts_that_round_trip():
    text = "hello from boston, how is the weather?  fine here!"
    parts = split("w1aw", "n0call", text, 37)
    assert [p.text for p in parts] == ["HELLO FROM BOSTON, HOW I", "S THE WEATHER? FINE HERE", "!"]
    assert [(p.index, p.total) for p in parts] == [(0, 3), (1, 3), (2, 3)]
    assert [unpack(pack(p)) for p in parts] == parts
    assert "".join(p.text for p in parts) == normalize(text)


def test_a_space_where_two_parts_join_survives():
    parts = split("W1AW", "", "A" * 23 + " B", 0)
    assert parts[0].text.endswith(" ")
    assert "".join(unpack(pack(p)).text for p in parts) == "A" * 23 + " B"


def test_part_layout_is_pinned():
    # the published format: version 1, from, to, number, part, parts - 1, 24 characters
    part = Part("W1AW", "N0CALL", 37, 1, 3, "S THE WEATHER? FINE HERE")
    assert pack(part).hex() == (
        "5612c148dc000036acfa6c5b00025125e704a250dd68dd82e38310bd75a861024d84"
    )


def test_a_blank_to_is_everyone():
    (part,) = split("W1AW", "", "CQ", 5)
    assert unpack(pack(part)).to == ""
    assert str(part) == "W1AW > ALL 1/1: CQ"


@pytest.mark.parametrize(
    "text, error",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("X" * 385, "385 characters; it can be 384"),
        ("price $5", r"outside A-Z 0-9 / - \. \? , ! ' : and space: '\$'"),
    ],
)
def test_rejects_messages_that_cannot_go(text, error):
    with pytest.raises(ValueError, match=error):
        split("W1AW", "N0CALL", text, 0)


def test_the_longest_message_fits():
    assert len(split("W1AW", "N0CALL", "X" * 384, 0)) == 16


def test_payloads_of_unknown_versions_are_rejected():
    data = bytearray(pack(Message("W1AW")))
    for version in (2, 3):
        data[0] = (data[0] & 0x3F) | version << 6
        crc = binascii.crc_hqx(bytes(data[:32]), 0xFFFF).to_bytes(2, "big")
        assert unpack(bytes(data[:32]) + crc) is None
