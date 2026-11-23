import re
import sys
from pathlib import Path

import numpy as np
import pytest

from undervoice import (
    FRAME,
    RATE,
    Message,
    Modulator,
    decode,
    encode,
    fm_channel,
    read_wav,
    write_wav,
)
from undervoice._cli import main
from undervoice._lpc import HOP
from undervoice._subaudible import FRAME as SUB_FRAME

SPEECH = Path(__file__).parent.parent / "speech" / "OSR_us_000_0010_8k.wav"
pytestmark = pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
MSG = Message("W1AW", "FN31pr", "ON THE AIR TESTING 123")


@pytest.fixture(autouse=True)
def no_sound_card(monkeypatch):
    # the CLI tests here stop before any audio; if one ever got further, it fails rather than
    # opening the real microphone and speakers
    monkeypatch.setitem(sys.modules, "sounddevice", None)


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


def test_cli_encode_then_decode_a_message(tmp_path, capsys):
    mixed = tmp_path / "mixed.wav"
    text = "Meet you on 146.52 at 7? Bring the antenna."
    args = ["--call", "W1AW", "--to", "n0call", "--msg", text]
    assert main(["encode", str(SPEECH), str(mixed), *args]) == 0
    assert main(["decode", str(mixed)]) == 0
    out = capsys.readouterr().out
    assert "W1AW > N0CALL 1/2: MEET YOU ON 146.52 AT 7?" in out
    [line] = [x for x in out.splitlines() if x.startswith("message: ")]
    assert re.fullmatch(
        r"message: \d{4}-\d\d-\d\d \d\d:\d\d  W1AW > N0CALL  "
        + re.escape("MEET YOU ON 146.52 AT 7? BRING THE ANTENNA."),
        line,
    )


def test_cli_decode_shows_a_message_cut_short_with_its_gap(speech, tmp_path, capsys):
    # room for two of the three parts
    short = tmp_path / "short.wav"
    write_wav(short, speech[: 2 * FRAME + RATE])
    mixed = tmp_path / "mixed.wav"
    args = ["--call", "W1AW", "--msg", "X" * 24 + "Y" * 24 + "Z"]
    assert main(["encode", str(short), str(mixed), *args]) == 0
    assert main(["decode", str(mixed)]) == 0
    [line] = [x for x in capsys.readouterr().out.splitlines() if x.startswith("message: ")]
    assert line.endswith("  W1AW > ALL  " + "X" * 24 + "Y" * 24 + "[...]  (missing part 3 of 3)")


@pytest.mark.parametrize(
    "argv, error",
    [
        (["tx", "--call", "W1AW", "--msg", "price $5"], "outside A-Z 0-9"),
        (["tx", "--call", "W1AW", "--to", "N0CALL"], "--to needs --msg"),
        (["tx", "--call", "W1AW", "--to", "TOOLONGCALL1", "--msg", "HI"], "to callsign"),
        (["encode", "in.wav", "out.wav", "--call", "W1AW", "--msg", "X" * 385], "385 characters"),
    ],
)
def test_cli_refuses_a_message_it_cannot_send(argv, error, capsys):
    # a parser error, before anything is read or any sound card opened
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 2
    assert error in capsys.readouterr().err


def test_pause_level_keeps_data_in_the_gaps(speech):
    clip = speech[: 10 * RATE]
    hop = 160
    levels = np.array([np.sqrt(np.mean(clip[i : i + hop] ** 2)) for i in range(0, len(clip), hop)])
    # the gaps between sentences: 30 dB or more below the loudest speech
    quiet = np.repeat(levels < 0.03 * levels.max(), hop)[: len(clip)]
    assert quiet.sum() > RATE // 2

    def rms(x):
        return np.sqrt(np.mean(x**2))

    plain = encode(clip, MSG) - clip
    floored = encode(clip, MSG, pause_db=-30.0) - clip
    # without the floor the data drops with the speech in the gaps (here only riding on the
    # recording's background noise); with it, it carries on at the floor
    assert rms(floored[quiet]) > 3 * rms(plain[quiet])
    # while speech is going the floor changes nothing
    loud = ~quiet
    np.testing.assert_allclose(rms(floored[loud]), rms(plain[loud]), rtol=0.2)


def test_pause_level_still_decodes(speech):
    frames = decode(encode(speech, MSG, pause_db=-40.0))
    assert [f.message for f in frames] == [MSG] * (len(speech) // FRAME)


def test_the_pause_floor_fades_about_4_db_every_10_seconds():
    talk = 0.1 * np.random.default_rng(1).standard_normal(3 * RATE)
    clip = np.concatenate([talk, np.zeros(30 * RATE)])
    data = encode(clip, MSG, pause_db=-30.0) - clip

    def level_db(seconds_into_pause):
        start = len(talk) + seconds_into_pause * RATE
        return 20 * np.log10(np.sqrt(np.mean(data[start : start + RATE // 2] ** 2)))

    # the recent speech level it follows is multiplied by 0.999 every block (Format, Shaping)
    assert level_db(15) - level_db(5) == pytest.approx(-4.3, abs=0.2)


def test_a_pause_level_above_the_data_level_is_refused():
    with pytest.raises(ValueError, match="can't be above the data level"):
        Modulator(MSG, -18.0, pause_db=-10.0)
    Modulator(MSG, -18.0, pause_db=-18.0)


@pytest.mark.parametrize(
    "argv",
    [
        ["encode", "in.wav", "out.wav", "--call", "W1AW", "--pause-level", "40"],
        ["tx", "--call", "W1AW", "--level", "-18", "--pause-level", "-10"],
        ["check", "--call", "W1AW", "--level", "-30", "--pause-level", "-25"],
        ["sweep", "in.wav", "--levels=-24,-18", "--pause-level", "-20"],
        ["abx", "in.wav", "--levels=-24,-18", "--pause-level", "-20"],
    ],
)
def test_cli_refuses_a_pause_level_above_the_data_level(argv, capsys):
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 2
    assert "can't be above the data level" in capsys.readouterr().err


def test_cli_lets_sub_mode_ignore_the_pause_level(tmp_path):
    out = tmp_path / "out.wav"
    args = ["--call", "W1AW", "--mode", "sub", "--pause-level", "-10"]
    assert main(["encode", str(SPEECH), str(out), *args]) == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["decode", "{wav}"],
        ["decode", "{wav}", "--mode", "sub"],
        ["encode", "{wav}", "{out}", "--call", "W1AW"],
        ["channel", "{wav}", "{out}"],
        ["sweep", "{wav}"],
        ["tx", "--play", "{wav}", "--call", "W1AW"],
        ["abx", "{wav}"],
    ],
)
def test_cli_says_which_wav_it_cannot_read(argv, tmp_path):
    missing = tmp_path / "nosuch.wav"
    filled = [a.format(wav=missing, out=tmp_path / "out.wav") for a in argv]
    with pytest.raises(SystemExit, match=re.escape(f"can't read {missing}: No such file")):
        main(filled)


def test_cli_says_when_a_file_is_not_a_wav(tmp_path):
    notes = tmp_path / "notes.wav"
    notes.write_text("not audio", encoding="utf-8")
    with pytest.raises(SystemExit, match=re.escape(f"can't read {notes}: File format")):
        main(["decode", str(notes)])


def test_cli_sub_mode_round_trip_needs_the_mode_on_both_ends(speech, tmp_path, capsys):
    mixed = tmp_path / "mixed.wav"
    assert main(["encode", str(SPEECH), str(mixed), "--call", "W1AW", "--mode", "sub"]) == 0
    assert main(["decode", str(mixed), "--mode", "sub"]) == 0
    assert capsys.readouterr().out.count("W1AW") == len(speech) // SUB_FRAME
    assert main(["decode", str(mixed)]) == 1
    assert capsys.readouterr().out.strip() == "no frames decoded"


def test_cli_channel_flat_path_carries_what_the_voice_path_loses(speech, tmp_path, capsys):
    # speech peaking at full scale, as with the mic level set right
    loud = tmp_path / "loud.wav"
    write_wav(loud, 0.99 * speech / np.max(np.abs(speech)))
    mixed = tmp_path / "mixed.wav"
    assert main(["encode", str(loud), str(mixed), "--call", "W1AW", "--mode", "sub"]) == 0

    def frames_through(path):
        rx = tmp_path / f"{path}.wav"
        assert main(["channel", str(mixed), str(rx), "--path", path, "--seed", "1"]) == 0
        main(["decode", str(rx), "--mode", "sub"])
        return capsys.readouterr().out.count("W1AW")

    flat = frames_through("flat")
    assert flat == len(speech) // SUB_FRAME
    assert frames_through("voice") < flat
