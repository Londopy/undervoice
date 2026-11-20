from pathlib import Path

import numpy as np
import pytest

from undervoice import RATE, Message, fm_channel, read_wav
from undervoice._subaudible import FRAME, Modulator, decode, encode

SPEECH_DIR = Path(__file__).parent.parent / "speech"
SPEECH = SPEECH_DIR / "OSR_us_000_0010_8k.wav"
pytestmark = pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
MSG = Message("W1AW", "FN31pr", "ON THE AIR TESTING 123")


def _loud(x):
    # the mic level set so speech peaks just reach full scale
    return x / np.max(np.abs(x))


def _data_port(tx, snr_db, rng, ppm=0.0):
    # fm_channel sets its gain from the 99.9th percentile of what it is sent; this drive keeps
    # that gain at 1, so full scale is full deviation, which is what level_db assumes
    drive_db = 20 * np.log10(np.percentile(np.abs(tx), 99.9))
    return fm_channel(tx, snr_db, drive_db, ppm=ppm, rng=rng, path="flat")


@pytest.fixture(scope="module")
def speech():
    return _loud(read_wav(SPEECH))


def test_clean_round_trip(speech):
    frames = decode(encode(speech, MSG))
    assert [f.message for f in frames] == [MSG] * (len(speech) // FRAME)
    starts = [f.start for f in frames]
    np.testing.assert_allclose(starts, FRAME * np.arange(len(frames)), atol=1.0)


def test_through_data_port_with_clock_drift(speech):
    rx = _data_port(encode(speech, MSG), 40, np.random.default_rng(7), ppm=100)
    assert [f.message for f in decode(rx)] == [MSG] * (len(speech) // FRAME)


def test_through_data_port_at_30_db(speech):
    rx = _data_port(encode(speech, MSG), 30, np.random.default_rng(2))
    assert [f.message for f in decode(rx)] == [MSG] * (len(speech) // FRAME)


def test_inverted_audio(speech):
    frames = decode(-encode(speech, MSG))
    assert [f.message for f in frames] == [MSG] * (len(speech) // FRAME)


def test_repeater_voice_path_stops_it(speech):
    # mic and speaker audio through a repeater: four 300 Hz high-passes in a row
    rx = fm_channel(encode(speech, MSG), snr_db=40, hops=2, rng=np.random.default_rng(3))
    assert decode(rx) == []


def test_plain_speech_has_no_frames(speech):
    assert decode(speech) == []


def test_streaming_matches_whole_file(speech):
    clip = speech[: 3 * RATE]
    rng = np.random.default_rng(1)
    mod = Modulator(MSG)
    out, i = [], 0
    while i < len(clip):
        n = int(rng.integers(0, 2000))
        out.append(mod.process(clip[i : i + n]))
        i += n
    streamed = np.concatenate(out)
    assert len(streamed) == len(clip)
    np.testing.assert_allclose(streamed, encode(clip, MSG), atol=1e-12)


def test_speech_is_high_passed_then_pre_emphasized_like_a_radio():
    t = np.arange(2 * RATE) / RATE
    # the data path ignores the speech, so taking the data alone away leaves the voice path
    data = Modulator(MSG).process(np.zeros(len(t)))

    def gain_db(freq):
        tone = 0.1 * np.sin(2 * np.pi * freq * t)
        voice = (Modulator(MSG).process(tone) - data)[RATE:]
        return 20 * np.log10(np.sqrt(np.mean(voice**2)) / np.sqrt(np.mean(tone[RATE:] ** 2)))

    assert gain_db(1000) == pytest.approx(0.0, abs=0.1)
    # the analog 750 us network's rise from 1 to 3 kHz, to within the high-pass's 0.5 dB ripple
    analog = abs(1 + 2j * np.pi * 3000 * 750e-6) / abs(1 + 2j * np.pi * 1000 * 750e-6)
    assert gain_db(3000) - gain_db(1000) == pytest.approx(20 * np.log10(analog), abs=0.5)
    # nothing of the voice is left where the data is
    assert max(gain_db(f) for f in (100, 150, 200, 250)) < -60


def test_default_level_has_the_power_of_a_sine_at_minus_18_dbfs():
    data = encode(np.zeros(4 * FRAME), MSG)
    # a sine of peak amplitude a has power a**2 / 2
    assert 10 * np.log10(np.mean(data**2)) == pytest.approx(-18 - 10 * np.log10(2), abs=0.2)


def test_frames_far_down_in_the_noise_are_still_found(speech):
    # about 6 dB of SNR per symbol, which scores well under a perfect sync match
    rx = _data_port(encode(speech, MSG, -42.0), 20, np.random.default_rng(0))
    assert [f.message for f in decode(rx)] == [MSG] * (len(speech) // FRAME)


@pytest.mark.slow
def test_frame_loss_through_data_port():
    # a measurement to quote, not a check on it: pytest -m slow -s -k frame_loss
    clips = [_loud(read_wav(path)) for path in sorted(SPEECH_DIR.glob("*.wav"))]
    snrs = [40, 30, 20, 10]
    rng = np.random.default_rng(0)
    rows = ["| Level | " + " | ".join(f"{s} dB" for s in snrs) + " |"]
    wrong = 0
    for level in [-48, -42, -36, -30, -24, -18]:
        cells = []
        for snr in snrs:
            sent = received = 0
            snr_db = []
            for clip in clips:
                tx = encode(np.roll(clip, int(rng.integers(len(clip)))), MSG, level)
                frames = decode(_data_port(tx, snr, rng))
                good = [f for f in frames if f.message == MSG]
                sent += len(tx) // FRAME
                received += min(len(good), len(tx) // FRAME)
                wrong += len(frames) - len(good)
                snr_db += [f.snr_db for f in good]
            margin = f" ({round(float(np.mean(snr_db)))} dB)" if snr_db else ""
            cells.append(f"{100 * (1 - received / sent):.0f}%{margin}")
        rows.append(f"| {level} dB | " + " | ".join(cells) + " |")
    print(f"\nframe loss through the data port, {sent} frames per cell (mean frame SNR)")
    print("\n".join(rows))
    assert wrong == 0
