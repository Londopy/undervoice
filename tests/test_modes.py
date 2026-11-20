from pathlib import Path

import numpy as np
import pytest

from undervoice import FRAME, RATE, Message, read_wav
from undervoice._modes import MODES, SUB, VOICE, decode, encode, mixer
from undervoice._subaudible import FRAME as SUB_FRAME

SPEECH = Path(__file__).parent.parent / "speech" / "OSR_us_000_0010_8k.wav"
pytestmark = pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
MSG = Message("W1AW", "FN31pr", "MODE CHECK")
FRAMES = {VOICE: FRAME, SUB: SUB_FRAME}


@pytest.fixture(scope="module")
def speech():
    return read_wav(SPEECH)[: 20 * RATE]


@pytest.mark.parametrize("mode", MODES)
def test_each_mode_decodes_its_own_data_and_not_the_other(speech, mode):
    mixed = encode(mode, speech, MSG, -18.0, 0.5)
    assert [f.message for f in decode(mode, mixed, 0.5)] == [MSG] * (len(speech) // FRAMES[mode])
    other = SUB if mode == VOICE else VOICE
    assert decode(other, mixed, 0.5) == []


@pytest.mark.parametrize("mode", MODES)
def test_the_streaming_mixer_sends_the_mode_s_data(speech, mode):
    mod = mixer(mode, MSG, -18.0, 0.5, None)
    sent = np.concatenate([mod.process(speech[i : i + 160]) for i in range(0, len(speech), 160)])
    assert [f.message for f in decode(mode, sent, 0.5)] == [MSG] * (len(speech) // FRAMES[mode])
