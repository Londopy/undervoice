from pathlib import Path

import numpy as np
import pytest

from undervoice import FRAME, RATE, Message, Modulator, decode, encode, fm_channel, read_wav
from undervoice._cli import main
from undervoice._lpc import HOP

SPEECH = Path(__file__).parent.parent / "speech" / "OSR_us_000_0010_8k.wav"
pytestmark = pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
MSG = Message("W1AW", "FN31pr", "ON THE AIR TESTING 123")


@pytest.fixture(scope="module")
def speech():
    return read_wav(SPEECH)


def test_clean_round_trip(speech):
    frames = decode(encode(speech, MSG))
    assert [f.message for f in frames] == [MSG] * (len(speech) // FRAME)
    assert [round(f.start / FRAME) for f in frames] == list(range(len(frames)))


def test_through_fm_channel_with_clock_drift(speech):
    rx = fm_channel(encode(speech, MSG, -18.0), snr_db=40, ppm=80, rng=np.random.default_rng(7))
    frames = decode(rx)
    assert [f.message for f in frames] == [MSG] * (len(speech) // FRAME)


def test_inverted_audio(speech):
    frames = decode(-encode(speech, MSG))
    assert [f.message for f in frames] == [MSG] * (len(speech) // FRAME)


def test_plain_speech_has_no_frames(speech):
    assert decode(speech) == []


def test_streaming_matches_whole_file(speech):
    clip = speech[: 3 * RATE]
    rng = np.random.default_rng(1)
    mod = Modulator(MSG)
    out, i = [], 0
    while i < len(clip):
        n = int(rng.integers(1, 2000))
        out.append(mod.process(clip[i : i + n]))
        i += n
    streamed = np.concatenate(out)
    assert len(streamed) == len(clip)
    np.testing.assert_allclose(streamed[HOP:], encode(clip, MSG)[:-HOP], atol=1e-12)


def test_defaults_through_fm_channel(speech):
    frames = decode(fm_channel(encode(speech, MSG), snr_db=40, rng=np.random.default_rng(2)))
    assert [f.message for f in frames] == [MSG] * (len(speech) // FRAME)


def test_clip_of_exactly_one_frame(speech):
    frames = decode(encode(speech[:FRAME], MSG))
    assert [f.message for f in frames] == [MSG]
    assert abs(frames[0].start) < 0.5


def test_rejects_shape_of_one():
    with pytest.raises(ValueError):
        Modulator(MSG, shape=1.0)


def test_cli_encode_then_decode(speech, tmp_path, capsys):
    mixed = tmp_path / "mixed.wav"
    args = ["--call", "W1AW", "--grid", "FN31pr", "--status", "CLI TEST"]
    assert main(["encode", str(SPEECH), str(mixed), *args]) == 0
    assert main(["decode", str(mixed)]) == 0
    assert capsys.readouterr().out.count("W1AW FN31pr CLI TEST") == len(speech) // FRAME
