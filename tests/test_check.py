import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import resample_poly

from undervoice import Message, read_wav
from undervoice._check import MicCheck, advise, finish, key_briefly
from undervoice._live import BLOCK
from undervoice._waveform import RATE

SPEECH = Path(__file__).parent.parent / "speech" / "OSR_us_000_0010_8k.wav"
MSG = Message("W1AW", "FN31pr", "MIC CHECK")


def test_advice_follows_the_level():
    rng = np.random.default_rng(0)
    talk = rng.standard_normal(RATE * 2)
    assert "clips" in advise(np.clip(talk, -1, 1))[2]
    assert "Almost nothing" in advise(talk * 1e-4)[2]
    assert "quiet" in advise(talk * 0.01)[2]
    assert "good" in advise(talk * 0.1)[2]


@pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
@pytest.mark.parametrize("mode", ["voice", "sub"])
def test_a_mic_recording_with_data_mixed_in_decodes(mode):
    recording = read_wav(SPEECH)[: 11 * RATE]
    result = finish(recording, MSG, -18.0, 0.5, None, mode)
    assert result.frames == 1
    assert len(result.mixed) == len(recording)


class FakeMic:
    def __init__(self, rate, blocksize, device, channels, callback):
        self.callback = callback

    def start(self):
        audio = resample_poly(np.full(RATE * 3, 0.25), 6, 1)

        def feed():
            for i in range(0, len(audio) - BLOCK, BLOCK):
                self.callback(audio[i : i + BLOCK, None], BLOCK, None, None)

        threading.Thread(target=feed, daemon=True).start()

    def stop(self):
        pass

    def close(self):
        pass


def test_mic_check_records_the_requested_length(monkeypatch):
    module = types.SimpleNamespace(InputStream=FakeMic, PortAudioError=OSError)
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    check = MicCheck(None, seconds=2.0)
    deadline = time.monotonic() + 5
    while not check.done and time.monotonic() < deadline:
        time.sleep(0.01)
    assert check.done
    audio = check.audio()
    assert len(audio) == 2 * RATE
    assert check.peak > 0.2


class FakeKeyer:
    name = "fake"

    def __init__(self):
        self.log = []

    def key(self, on):
        self.log.append(on)

    def close(self):
        pass


def test_ptt_test_always_unkeys(monkeypatch):
    keyer = FakeKeyer()
    key_briefly(keyer, seconds=0.01)
    assert keyer.log == [True, False]

    def interrupted(seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(time, "sleep", interrupted)
    keyer = FakeKeyer()
    with pytest.raises(KeyboardInterrupt):
        key_briefly(keyer)
    assert keyer.log == [True, False]
