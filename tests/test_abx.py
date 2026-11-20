import csv
import io
import re
import sys
import types
from pathlib import Path

import numpy as np
import pytest
from scipy.io import wavfile
from scipy.signal import lfilter

from undervoice import RATE, Message, encode, fm_channel, read_wav
from undervoice._abx import Answer, export, load, make_trials, report, run, save, speaker
from undervoice._cli import main
from undervoice._ptt import SetupError

SPEECH = Path(__file__).parent.parent / "speech" / "OSR_us_000_0010_8k.wav"
needs_speech = pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
MSG = Message("N0CALL", "FN31pr", "LISTENING TEST")
LEVELS = [-27.0, -18.0]


@pytest.fixture(scope="module")
def speech():
    return read_wav(SPEECH)


@pytest.fixture(scope="module")
def trials(speech):
    # 30 dB leaves the hiss plainly audible, so different noise on A and B could not hide
    return make_trials([speech], LEVELS, 0.5, 30.0, 10, np.random.default_rng(1))


def verdicts(text):
    """level -> (answers, correct, p, verdict), read from the lines meant for an issue."""
    pattern = (
        r"ABX at (\S+) dB: (\d+) of (\d+) correct \(\d+%\), p = (\S+), (heard|not heard|too few)$"
    )
    return {
        float(m[1]): (int(m[3]), int(m[2]), float(m[4]), m[5])
        for m in re.finditer(pattern, text, re.MULTILINE)
    }


class SharpEar:
    """Plays A, B and X, then picks whichever of A and B is exactly X."""

    def __init__(self):
        self.heard = []
        self.keys = []
        self.asked = 0

    def play(self, clip):
        self.heard.append(clip)

    def ask(self, prompt):
        self.asked += 1
        assert self.asked < 1000, "the session never ended"
        if not self.keys:
            self.heard = []
            self.keys = ["a", "b", "x", "answer"]
        key = self.keys.pop(0)
        if key != "answer":
            return key
        a, b, x = self.heard
        assert np.array_equal(x, a) != np.array_equal(x, b)
        return "1" if np.array_equal(x, a) else "2"


@needs_speech
def test_clips_are_the_sentences_of_the_recording(speech, trials):
    # the recording is ten Harvard sentences with pauses between them
    clips = sorted({(t.start, t.start + len(t.a)) for t in trials})
    assert len(clips) == 10
    assert all(end <= start for (_, end), (start, _) in zip(clips, clips[1:]))
    for start, end in clips:
        assert 2 * RATE <= end - start <= 4 * RATE
        source = speech[start:end]
        loud = np.max(np.abs(source))
        assert np.max(np.abs(source[: RATE // 5])) < 0.2 * loud
        assert np.max(np.abs(source[-RATE // 5 :])) < 0.2 * loud


@needs_speech
def test_too_few_sentences_falls_back_to_fixed_windows(speech):
    windows = make_trials([speech], [-18.0], 0.5, 30.0, 11, np.random.default_rng(2))
    assert len(windows) == 11
    assert {len(t.a) for t in windows} == {3 * RATE}
    assert len({t.start for t in windows}) == 11


@needs_speech
def test_a_and_b_differ_only_by_the_data(speech, trials):
    mixed = {level: encode(speech, MSG, level, 0.5) for level in LEVELS}
    for t in trials:
        n = len(t.a)
        data = mixed[t.level_db][t.start : t.start + n] - speech[t.start : t.start + n]
        # the data on its own through a clean channel, driven too gently to clip
        heard = fm_channel(data, snr_db=200.0, drive_db=-20.0, rng=np.random.default_rng(0))
        r = np.corrcoef(t.a - t.b, heard)[0, 1]
        # A - B is the data when A has it and minus the data when B has it
        assert (r if t.data_was == "A" else -r) > 0.9
        plain = t.b if t.data_was == "A" else t.a
        assert np.linalg.norm(t.a - t.b) < 3 * 10 ** (t.level_db / 20) * np.linalg.norm(plain)


def bursts():
    """Four bursts of speech-like noise with a second of digital silence around each."""
    rng = np.random.default_rng(3)
    burst = 0.1 * lfilter([1.0], [1.0, -0.9], rng.standard_normal(5 * RATE // 2))
    hush = np.zeros(RATE)
    return np.concatenate([hush, burst, hush, -burst, hush, burst[::-1], hush, 0.5 * burst, hush])


def test_where_there_is_no_data_a_and_b_are_the_same_hiss():
    # digital silence carries no data, so each clip opens with a quarter second of hiss alone
    trials = make_trials([bursts()], [-18.0], 0.5, 30.0, 4, np.random.default_rng(5))
    lead = slice(RATE // 100, RATE // 5)
    for t in trials:
        assert np.linalg.norm((t.a - t.b)[lead]) < 0.01 * np.linalg.norm(t.a[lead])


def test_with_a_pause_level_the_data_carries_on_into_the_silence():
    trials = make_trials([bursts()], [-18.0], 0.5, 30.0, 4, np.random.default_rng(5), -30.0)
    lead = slice(RATE // 100, RATE // 5)
    heard = [
        np.linalg.norm((t.a - t.b)[lead]) > 0.05 * np.linalg.norm(t.a[lead])
        for t in sorted(trials, key=lambda t: t.start)
    ]
    # the first burst has no speech before it to hold a level for the pause
    assert heard == [False, True, True, True]


@needs_speech
def test_x_and_the_data_each_land_on_both_sides(trials):
    assert {t.x_was for t in trials} == {"A", "B"}
    assert {t.data_was for t in trials} == {"A", "B"}


@needs_speech
def test_a_listener_who_can_tell_them_apart_is_heard(trials):
    ear = SharpEar()
    answers = run(trials, ear.play, ear.ask)
    assert len(answers) == len(trials)
    assert all(a.correct for a in answers)
    # 10 of 10 by guessing has a chance of 1 in 1024
    assert verdicts(report(answers)) == {
        level: (10, 10, pytest.approx(1 / 1024, rel=1e-2), "heard") for level in LEVELS
    }


@needs_speech
def test_a_guesser_is_not_heard(trials):
    guesses = iter(str(k) for k in np.random.default_rng(4).integers(1, 3, len(trials)))
    answers = run(trials, lambda clip: None, lambda prompt: next(guesses))
    found = verdicts(report(answers))
    assert sorted(found) == LEVELS
    assert {(n, verdict) for n, _, _, verdict in found.values()} == {(10, "not heard")}


@needs_speech
def test_q_stops_early_and_the_report_counts_only_answers_given(trials, capsys):
    keys = iter(["a", "1", "z", "x", "2", "bx", "1", "q"])
    played = []
    answers = run(trials, played.append, lambda prompt: next(keys))
    assert [(a.trial, a.answer) for a in answers] == [(1, "A"), (2, "B"), (3, "A")]
    expected = [trials[0].a, trials[1].x, trials[2].b, trials[2].x]
    assert len(played) == 4
    assert all(np.array_equal(p, e) for p, e in zip(played, expected))
    found = verdicts(report(answers))
    assert sum(n for n, _, _, _ in found.values()) == 3
    assert {verdict for _, _, _, verdict in found.values()} == {"too few"}
    assert "1 means X is A" in capsys.readouterr().out


@needs_speech
def test_closed_input_stops_like_q(trials):
    def ask(prompt):
        raise EOFError

    assert run(trials, lambda clip: None, ask) == []
    assert report([]) == "no answers"


@needs_speech
def test_a_sound_card_error_keeps_the_session_going(trials, capsys):
    played = []

    def play(clip):
        played.append(clip)
        if len(played) == 1:
            raise SetupError("can't open the sound card: device unavailable")

    keys = iter(["a", "a", "1", "q"])
    answers = run(trials, play, lambda prompt: next(keys))
    assert len(played) == 2
    assert [a.answer for a in answers] == ["A"]
    assert "device unavailable" in capsys.readouterr().out


def answer(level, trial, x_was, said, seconds=4.2):
    return Answer(level, trial, f"1:{trial}.00-{trial + 2}.50", "A", x_was, said, seconds)


def test_verdict_needs_eight_answers_and_p_under_5_percent():
    seven_of_eight = [answer(-21.0, i, "A", "A") for i in range(7)] + [answer(-21.0, 8, "A", "B")]
    six_of_eight = [answer(-24.0, i, "B", "B") for i in range(6)]
    six_of_eight += [answer(-24.0, i, "B", "A") for i in (7, 8)]
    seven_of_seven = [answer(-27.5, i, "A", "A") for i in range(7)]
    found = verdicts(report(seven_of_eight + six_of_eight + seven_of_seven))
    # P(at least k of n right by guessing): 9/256, 37/256 and 1/128
    assert found == {
        -21.0: (8, 7, pytest.approx(9 / 256, rel=1e-2), "heard"),
        -24.0: (8, 6, pytest.approx(37 / 256, rel=1e-2), "not heard"),
        -27.5: (7, 7, pytest.approx(1 / 128, rel=1e-2), "too few"),
    }


def test_saved_answers_load_back_and_a_second_run_adds_to_them(tmp_path):
    first = [
        answer(-18.0, 1, "A", "A"),
        answer(-18.0, 2, "B", "A", 12.3),
        answer(-21.5, 3, "A", "A"),
    ]
    second = [answer(-18.0, 1, "B", "B", 7.0)]
    path = tmp_path / "results.csv"
    save(first, path)
    assert load([path]) == first
    save(second, path)
    assert load([path]) == first + second
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "level_db,trial,sentence,data_was,x_was,answer,correct,seconds"
    assert len(lines) == 5
    with open(path, newline="", encoding="utf-8") as f:
        assert [row["correct"] for row in csv.DictReader(f)] == ["1", "0", "1", "1"]
    other = tmp_path / "someone-else.csv"
    save(second, other)
    assert load([path, other]) == first + second + second


def test_results_files_are_checked(tmp_path):
    key = tmp_path / "key.csv"
    key.write_text(
        "level_db,trial,sentence,data_was,x_was\n-18.0,1,1:0.27-2.88,A,B\n", encoding="utf-8"
    )
    before = key.read_bytes()
    with pytest.raises(ValueError, match="not an ABX results file"):
        save([answer(-18.0, 1, "A", "A")], key)
    assert key.read_bytes() == before
    bad = tmp_path / "bad.csv"
    save([answer(-18.0, 1, "A", "A"), answer(-18.0, 2, "A", "C")], bad)
    with pytest.raises(ValueError, match="bad.csv line 3"):
        load([bad])


@needs_speech
def test_export_writes_the_clips_and_a_key_that_matches(trials, tmp_path):
    folder = tmp_path / "abx"
    export(trials[:3], folder)
    names = ["key.csv"] + [f"{i:03d}_{c}.wav" for i in (1, 2, 3) for c in "ABX"]
    assert sorted(p.name for p in folder.iterdir()) == sorted(names)
    with open(folder / "key.csv", newline="", encoding="utf-8") as f:
        key = list(csv.DictReader(f))
    assert len(key) == 3
    for i, (t, row) in enumerate(zip(trials, key), 1):
        assert row == {
            "level_db": str(t.level_db),
            "trial": str(i),
            "sentence": t.sentence,
            "data_was": t.data_was,
            "x_was": t.x_was,
        }
        clips = {}
        for c in "ABX":
            rate, clips[c] = wavfile.read(folder / f"{i:03d}_{c}.wav")
            assert rate == RATE
        np.testing.assert_array_equal(clips["X"], clips[row["x_was"]])
        np.testing.assert_allclose(clips["A"] / 32767, t.a, atol=1e-4)
        np.testing.assert_allclose(clips["B"] / 32767, t.b, atol=1e-4)


def fake_sounddevice(monkeypatch, played, check=lambda **settings: None):
    module = types.SimpleNamespace(
        PortAudioError=type("PortAudioError", (Exception,), {}),
        check_output_settings=check,
        play=lambda data, rate, device, blocking: played.append((data, rate, device)),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", module)


def test_speaker_plays_clips_at_48_khz_on_the_chosen_device(monkeypatch):
    played = []
    fake_sounddevice(monkeypatch, played)
    tone = 0.5 * np.sin(2 * np.pi * 1000 * np.arange(RATE) / RATE)
    speaker("Headphones")(tone)
    [(data, rate, device)] = played
    assert (rate, device, len(data)) == (48000, "Headphones", 48000)
    t = np.arange(4800, 43200) / rate
    np.testing.assert_allclose(data[4800:43200], 0.5 * np.sin(2 * np.pi * 1000 * t), atol=0.01)


def test_speaker_rejects_a_missing_device_before_any_clip(monkeypatch):
    def check(**settings):
        raise ValueError(f"No output device matching {settings['device']!r}")

    played = []
    fake_sounddevice(monkeypatch, played, check)
    with pytest.raises(SetupError, match="No output device matching 'Nope'"):
        speaker("Nope")
    assert played == []


@needs_speech
@pytest.mark.parametrize(
    "save, problem",
    [("notes.csv", "is not an ABX results file"), ("gone/results.csv", "there is no folder")],
)
def test_abx_checks_where_it_saves_before_the_listener_starts(monkeypatch, tmp_path, save, problem):
    played = []
    fake_sounddevice(monkeypatch, played)
    # a listener who plays X and answers every trial
    monkeypatch.setattr(sys, "stdin", io.StringIO("x\n1\n" * 10))
    notes = tmp_path / "notes.csv"
    notes.write_text("a,b\n1,2\n", encoding="utf-8")
    args = [str(SPEECH), "--levels=-24", "--trials", "3", "--save", str(tmp_path / save)]
    with pytest.raises(SystemExit, match=problem):
        main(["abx", *args])
    assert played == []
    assert notes.read_text(encoding="utf-8") == "a,b\n1,2\n"
