from pathlib import Path

import numpy as np
import pytest

from undervoice import Message, decode, read_wav
from undervoice._lpc import HOP
from undervoice._message import Part, split
from undervoice._messages import Inbox
from undervoice._modes import VOICE, mix
from undervoice._modulator import Modulator
from undervoice._waveform import FRAME, RATE

SPEECH = Path(__file__).parent.parent / "speech" / "OSR_us_000_0010_8k.wav"
STATION = Message("W1AW", "FN31pr", "STATUS")
PARTS = split("W1AW", "N0CALL", "MEET YOU ON 146.52 AT 7? BRING THE ANTENNA", 4)


def sent(mod):
    out = []
    while not mod.frames.sent.empty():
        out.append(mod.frames.sent.get_nowait())
    return out


@pytest.mark.parametrize(
    "quiet",
    [np.zeros(3 * FRAME), 10 ** (-50 / 20) * np.random.default_rng(0).standard_normal(3 * FRAME)],
    ids=["silence", "hiss at -50 dBFS"],
)
def test_keying_without_talking_sends_no_part(quiet):
    mod = Modulator(STATION)
    mod.frames.send(PARTS)
    mod.process(quiet)
    assert sent(mod) == []
    assert mod.frames.waiting == PARTS


@pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
def test_a_part_that_went_out_in_silence_goes_again_once_there_is_speech():
    speech = read_wav(SPEECH)
    audio = np.concatenate([np.zeros(12 * RATE), speech, speech])
    mod = Modulator(STATION)
    mod.frames.send(PARTS)
    mixed = mod.process(audio)
    assert sent(mod) == PARTS
    inbox = Inbox()
    for f in decode(mixed[RATE // 50 :]):
        if isinstance(f.message, Part):
            inbox.add(f.message)
    # a part from a frame that was partly silent can go twice; the receiver files it once
    assert [m.text for m in inbox.messages] == ["MEET YOU ON 146.52 AT 7? BRING THE ANTENNA"]


def test_a_quiet_mic_still_counts_once_it_carries_enough():
    mod = Modulator(STATION)
    mod.frames.send(PARTS[:1])
    mod.process(10 ** (-30 / 20) * np.random.default_rng(1).standard_normal(FRAME + RATE))
    assert sent(mod) == PARTS[:1]


def hiss(db):
    x = np.random.default_rng(7).standard_normal(3 * FRAME)
    return 10 ** (db / 20) * x / np.sqrt(np.mean(x**2))


@pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
@pytest.mark.parametrize("talk", [0.2, 2.0, 4.0])
def test_a_few_seconds_of_speech_then_silence_sends_no_part(talk):
    # a frame like this doesn't decode: the receiver locks on across the whole frame
    audio = np.zeros(3 * FRAME)
    audio[: int(talk * RATE)] = read_wav(SPEECH)[RATE : RATE + int(talk * RATE)]
    mod = Modulator(STATION)
    mod.frames.send(PARTS[:1])
    mod.process(audio)
    assert sent(mod) == []


@pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
def test_restart_forgets_what_the_abandoned_frame_carried():
    speech = np.tile(read_wav(SPEECH), 2)
    mod = Modulator(STATION)
    mod.frames.send(PARTS[:1])
    for i in range(0, 4 * FRAME // 5, HOP):
        mod.process(speech[i : i + HOP])
    mod.restart()
    mod.process(np.zeros(FRAME + 2 * HOP))
    assert sent(mod) == []


@pytest.mark.parametrize("db, counted", [(-43.0, True), (-45.0, False)])
def test_a_part_counts_just_above_sent_db_and_not_just_below(db, counted):
    # the data runs 21 dB under the hiss, so these carry -64 and -66 dB re full scale
    mod = Modulator(STATION)
    mod.frames.send(PARTS[:1])
    mod.process(hiss(db))
    assert bool(sent(mod)) is counted


@pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
@pytest.mark.parametrize("extra", [-HOP + 1, 0, 7])
def test_a_recording_one_frame_long_carries_one_part_and_keeps_its_length(extra):
    audio = np.tile(read_wav(SPEECH), 2)[: FRAME + extra]
    mixed, gone = mix(VOICE, audio, STATION, -21.0, 0.5, None, PARTS[:1])
    assert len(mixed) == len(audio)
    assert gone == PARTS[:1]
