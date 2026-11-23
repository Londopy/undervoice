from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import resample_poly

from undervoice import RATE, Message, fm_channel, read_wav
from undervoice._frames import Frames
from undervoice._live import BLOCK, _Sender
from undervoice._message import Part, split
from undervoice._messages import Inbox, append, heard_line
from undervoice._modes import MODES, SUB, decode, encode
from undervoice._subaudible import FRAME as SUB_FRAME

SPEECH = Path(__file__).parent.parent / "speech" / "OSR_us_000_0010_8k.wav"
STATION = Message("W1AW", "FN31pr", "STATUS")
TEXT = "hello from boston, how is the weather? fine here!"
T0 = datetime(2026, 11, 20, 7, 12, tzinfo=UTC)


def test_parts_heard_out_of_order_make_the_message():
    inbox = Inbox()
    a, b, c = split("W1AW", "N0CALL", TEXT, 37)
    for part in (c, a):
        inbox.add(part, T0)
    (m,) = inbox.messages
    assert not m.complete
    assert m.missing == [1]
    assert m.text == "HELLO FROM BOSTON, HOW I[...]!"
    assert inbox.add(b, T0) is m
    assert m.complete
    assert m.text == "HELLO FROM BOSTON, HOW IS THE WEATHER? FINE HERE!"


def test_a_part_heard_again_changes_nothing():
    inbox = Inbox()
    (part,) = split("W1AW", "N0CALL", "HI", 3)
    assert inbox.add(part, T0) is not None
    assert inbox.add(part, T0 + timedelta(minutes=1)) is None
    assert len(inbox.messages) == 1


@pytest.mark.parametrize(
    "later, when",
    [
        (split("W1AW", "N0CALL", "SOMETHING ELSE", 3)[0], T0),
        (split("W1AW", "N0CALL", "X" * 30, 3)[0], T0),
        (split("W1AW", "K1ABC", "HI", 3)[0], T0),
        (split("W1AW", "N0CALL", "HI", 3)[0], T0 + timedelta(hours=2)),
    ],
    ids=["other text", "other length", "other to", "an hour on"],
)
def test_a_reused_number_starts_a_new_message(later, when):
    inbox = Inbox()
    inbox.add(split("W1AW", "N0CALL", "HI", 3)[0], T0)
    assert inbox.add(later, when) is inbox.messages[1]


def test_a_slow_message_holds_together_while_each_part_follows_within_the_hour():
    inbox = Inbox()
    for minutes, part in zip((0, 50, 100), split("W1AW", "N0CALL", TEXT, 37), strict=True):
        inbox.add(part, T0 + timedelta(minutes=minutes))
    (m,) = inbox.messages
    assert m.complete


def test_once_a_number_is_used_again_its_older_message_takes_no_more_parts():
    inbox = Inbox()
    first, second = split("W1AW", "N0CALL", "X" * 30, 3)
    inbox.add(first, T0)
    inbox.add(split("W1AW", "N0CALL", "HI", 3)[0], T0)
    inbox.add(second, T0)
    assert len(inbox.messages) == 3
    assert inbox.messages[0].missing == [1]


def test_the_same_number_from_two_stations_is_two_messages():
    inbox = Inbox()
    inbox.add(split("W1AW", "", "HI", 3)[0], T0)
    inbox.add(split("K1ABC", "", "HI", 3)[0], T0)
    assert [m.sender for m in inbox.messages] == ["W1AW", "K1ABC"]


def test_the_log_is_plain_lines_in_utc(tmp_path):
    inbox = Inbox()
    for part in split("W1AW", "N0CALL", "X" * 60, 3)[::2]:
        inbox.add(part, T0)
    # 7:12 UTC, heard where the clocks read 2:12
    inbox.add(split("K1ABC", "", "CQ CQ", 4)[0], T0.astimezone(timezone(timedelta(hours=-5))))
    log = tmp_path / "messages.txt"
    for m in inbox.messages:
        append(log, heard_line(m))
    assert log.read_text(encoding="utf-8").splitlines() == [
        "2026-11-20 07:12Z  W1AW > N0CALL  "
        + "X" * 24
        + "[...]XXXXXXXXXXXX  (missing part 2 of 3)",
        "2026-11-20 07:12Z  K1ABC > ALL  CQ CQ",
    ]


class Wave:
    """Frames that render a payload as itself, to see what goes out in what order."""

    def __init__(self, parts):
        self.frames = Frames(STATION, lambda payload: payload)
        self.frames.send(parts)

    def frame(self, whole=True):
        out = self.frames.start()
        if whole:
            self.frames.finish()
        return out


def test_parts_go_in_order_then_the_station_frame():
    parts = split("W1AW", "N0CALL", TEXT, 1)
    w = Wave(parts)
    station = Frames(STATION, lambda payload: payload).start()
    sent = [w.frame() for _ in range(4)]
    assert sent[3] == station
    assert len(set(sent)) == 4
    assert [w.frames.sent.get_nowait() for _ in parts] == parts


def test_a_part_cut_off_goes_again():
    parts = split("W1AW", "N0CALL", TEXT, 1)
    w = Wave(parts)
    first = w.frame(whole=False)
    assert w.frames.waiting == parts
    assert w.frame() == first
    assert w.frames.waiting == parts[1:]


def test_cancel_drops_what_has_not_gone():
    w = Wave(split("W1AW", "N0CALL", TEXT, 1))
    w.frame(whole=False)
    w.frames.cancel()
    w.frames.finish()
    assert w.frames.waiting == []
    assert w.frames.sent.empty()


def test_a_part_cancelled_mid_frame_does_not_count_the_next_one_as_sent():
    w = Wave(split("W1AW", "N0CALL", "FIRST", 1))
    w.frame(whole=False)
    w.frames.cancel()
    later = split("W1AW", "N0CALL", "SECOND", 2)
    w.frames.send(later)
    # the cancelled part's frame ends; the new part has not gone out at all
    w.frames.finish()
    assert w.frames.waiting == later
    assert w.frames.sent.empty()


@pytest.mark.parametrize("mode", MODES)
def test_queued_parts_carry_over_to_the_next_over(mode):
    sender = _Sender(STATION, -18.0, 0.5, 0.0, mode=mode)
    parts = split("W1AW", "N0CALL", TEXT, 1)
    sender.mod.frames.send(parts)
    sender.key()
    sender.unkey()
    sender.key()
    assert sender.mod.frames.waiting == parts


def on_air(sender, seconds):
    """What the sender puts out over this many seconds of a silent mic, back at RATE."""
    out = np.zeros((BLOCK, 1))
    sent = []
    for _ in range(int(seconds * 6 * RATE) // BLOCK):
        sender.callback(np.zeros((BLOCK, 1)), out, BLOCK, None, None)
        sent.append(out[:, 0].copy())
    return resample_poly(np.concatenate(sent), 1, 6)


def test_a_part_cut_off_mid_frame_goes_again_from_its_start_next_over():
    # sub-audible, whose data goes out whether or not anyone talks
    sender = _Sender(STATION, -18.0, 0.5, 0.0, mode=SUB)
    parts = split("W1AW", "N0CALL", "HI", 1)
    sender.mod.frames.send(parts)
    sender.key()
    on_air(sender, SUB_FRAME / 2 / RATE)
    sender.unkey()
    assert sender.mod.frames.sent.empty()
    sender.key()
    heard = [f.message for f in decode(SUB, on_air(sender, SUB_FRAME / RATE + 1), 0.5)]
    assert heard == parts
    assert sender.mod.frames.sent.get_nowait() == parts[0]


@pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
@pytest.mark.parametrize("mode", MODES)
def test_a_message_goes_through_the_fm_channel(mode):
    speech = read_wav(SPEECH)
    parts = split("W1AW", "N0CALL", "HELLO, CAN YOU READ THIS? IT IS SENT UNDER MY VOICE.", 9)
    mixed = encode(mode, speech, STATION, -18.0, 0.5, None, parts)
    if mode == SUB:
        mixed = fm_channel(
            0.99 * mixed / np.max(np.abs(mixed)), 30, path="flat", rng=np.random.default_rng(3)
        )
    else:
        mixed = fm_channel(mixed, 40, rng=np.random.default_rng(3))
    heard = [f.message for f in decode(mode, mixed, 0.5)]
    assert heard[: len(parts)] == parts
    assert all(m == STATION for m in heard[len(parts) :])
    inbox = Inbox()
    for p in heard:
        if isinstance(p, Part):
            inbox.add(p, T0)
    assert inbox.messages[0].text == "HELLO, CAN YOU READ THIS? IT IS SENT UNDER MY VOICE."
