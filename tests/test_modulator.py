from pathlib import Path

import numpy as np
import pytest

from undervoice import Message, decode, read_wav
from undervoice._message import split
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
    heard = [f.message for f in decode(mixed[RATE // 50 :])]
    assert heard[: len(PARTS)] == PARTS


def test_a_quiet_mic_still_counts_once_it_carries_enough():
    mod = Modulator(STATION)
    mod.frames.send(PARTS[:1])
    mod.process(10 ** (-30 / 20) * np.random.default_rng(1).standard_normal(FRAME + RATE))
    assert sent(mod) == PARTS[:1]
