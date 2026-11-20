"""Blind ABX listening test: speech with data under it against the same speech without."""

from __future__ import annotations

import csv
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly
from scipy.stats import binomtest

from ._audio import write_wav
from ._channel import fm_channel
from ._live import CARD_RATE, Device, _open, _sd
from ._lpc import HOP
from ._message import Message
from ._modulator import encode
from ._ptt import SetupError
from ._waveform import RATE

COLUMNS = ["level_db", "trial", "sentence", "data_was", "x_was", "answer", "correct", "seconds"]


@dataclass(frozen=True, eq=False)
class Trial:
    """Clips A and B of one sentence, one of them with data under the speech, and which of
    them X is."""

    level_db: float
    recording: int
    start: int
    a: np.ndarray
    b: np.ndarray
    data_was: str
    x_was: str

    @property
    def x(self) -> np.ndarray:
        return self.a if self.x_was == "A" else self.b

    @property
    def sentence(self) -> str:
        """The recording's number, counting from 1, and the clip's start and end in seconds."""
        end = self.start + len(self.a)
        return f"{self.recording + 1}:{self.start / RATE:.2f}-{end / RATE:.2f}"


@dataclass(frozen=True)
class Answer:
    """What the listener said X was in one trial, "A" or "B", and what it really was."""

    level_db: float
    trial: int
    sentence: str
    data_was: str
    x_was: str
    answer: str
    seconds: float

    @property
    def correct(self) -> bool:
        return self.answer == self.x_was


def _sentences(x: np.ndarray) -> list[tuple[int, int]]:
    """(start, end) of each stretch of speech between pauses of at least 0.3 s, with a
    quarter second of the pause kept on each side, for the ones that come out 2 to 4 s."""
    if len(x) < 2 * RATE:
        return []
    blocks = len(x) // HOP
    level = np.sqrt(np.mean(x[: blocks * HOP].reshape(blocks, HOP) ** 2, axis=1))
    # halfway in dB between the background and the speech; the floor is for recordings with
    # digital silence between sentences
    background = max(float(np.percentile(level, 10)), 1e-5)
    quiet = level < np.sqrt(background * np.percentile(level, 95))
    edges = np.flatnonzero(np.diff(np.concatenate([[1], quiet, [1]])))
    spans: list[list[int]] = []
    for start, end in zip(edges[::2], edges[1::2]):
        if spans and (start - spans[-1][1]) * HOP < 3 * RATE // 10:
            spans[-1][1] = int(end)
        else:
            spans.append([int(start), int(end)])
    found = []
    for start, end in spans:
        a, b = max(start * HOP - RATE // 4, 0), min(end * HOP + RATE // 4, len(x))
        if 2 * RATE <= b - a <= 4 * RATE:
            found.append((a, b))
    return found


def _clips(speech: Sequence[np.ndarray], per_level: int) -> list[tuple[int, int, int]]:
    """(recording, start, end) of every sentence, or of back to back 3 s windows when there
    are fewer sentences than one level needs."""
    clips = [(i, a, b) for i, x in enumerate(speech) for a, b in _sentences(x)]
    if len(clips) < per_level:
        n = 3 * RATE
        clips = [(i, a, a + n) for i, x in enumerate(speech) for a in range(0, len(x) - n + 1, n)]
    if not clips:
        raise ValueError("no speech to test with: every recording is shorter than 3 s")
    return clips


def _render(
    plain: np.ndarray, mixed: np.ndarray, snr_db: float, seed: int
) -> tuple[np.ndarray, np.ndarray]:
    """Both versions of a clip as heard through the FM channel, under the same noise."""
    n = len(plain)
    # fm_channel sets its mic gain from the peaks of what it is given, and the data moves
    # them, so send each version followed by the other: both get the same gain, and the same
    # noise over their first n samples
    p = fm_channel(np.concatenate([plain, mixed]), snr_db, rng=np.random.default_rng(seed))[:n]
    w = fm_channel(np.concatenate([mixed, plain]), snr_db, rng=np.random.default_rng(seed))[:n]
    # it also scales each output to its own loudest sample, so match the two speech levels
    w = w * (p @ p) / (w @ p)
    # 10 ms fades, so the clips start and stop without a click
    ramp = np.linspace(0.0, 1.0, RATE // 100)
    fade = np.concatenate([ramp, np.ones(n - 2 * len(ramp)), ramp[::-1]])
    return p * fade, w * fade


def make_trials(
    speech: Sequence[np.ndarray],
    levels: Sequence[float],
    shape: float,
    snr_db: float,
    per_level: int,
    rng: np.random.Generator,
) -> list[Trial]:
    """per_level trials at each level, shuffled together. Each is a sentence from the speech
    with data under it at that level and without, both through the FM channel at snr_db."""
    message = Message("N0CALL", "FN31pr", "LISTENING TEST")
    clips = _clips(speech, per_level)
    # one shuffled deck for every level, so a sentence repeats only once all have been used
    deck = rng.permutation(len(clips))
    trials = []
    for i, level in enumerate(levels):
        # the data runs through the whole recording, as it would on air, and clips are cut
        # from that
        mixed = [encode(x, message, level, shape) for x in speech]
        for j in range(per_level):
            rec, start, end = clips[deck[(i * per_level + j) % len(clips)]]
            plain, data = _render(
                speech[rec][start:end], mixed[rec][start:end], snr_db, int(rng.integers(2**32))
            )
            data_was = "AB"[int(rng.integers(2))]
            a, b = (data, plain) if data_was == "A" else (plain, data)
            trials.append(Trial(level, rec, start, a, b, data_was, "AB"[int(rng.integers(2))]))
    return [trials[k] for k in rng.permutation(len(trials))]


def speaker(device: Device = None) -> Callable[[np.ndarray], None]:
    """A play function for run() that sends clips to a sound card output at 48 kHz."""
    sd = _sd()
    # a wrong device name fails here rather than at the first clip
    _open(sd.check_output_settings, device=device, channels=1, samplerate=CARD_RATE)

    def play(clip: np.ndarray) -> None:
        audio = resample_poly(clip, CARD_RATE // RATE, 1).astype(np.float32)
        _open(sd.play, audio, CARD_RATE, device=device, blocking=True)

    return play


def _listen(
    trial: Trial, prompt: str, play: Callable[[np.ndarray], None], ask: Callable[[str], str]
) -> str | None:
    """Play clips until the listener says which one X is: "A", "B", or None to stop."""
    clips = {"a": trial.a, "b": trial.b, "x": trial.x}
    while True:
        try:
            key = ask(prompt).strip().lower()
        except EOFError:
            # stdin closed (Ctrl+D, or Ctrl+Z and Enter on Windows): same as q
            return None
        if key in ("1", "2"):
            return "AB"[int(key) - 1]
        if key == "q":
            return None
        if not key or not set(key) <= set(clips):
            print("a, b or x plays that clip; 1 means X is A, 2 means X is B; q stops")
            continue
        for name in key:
            try:
                play(clips[name])
            except SetupError as e:
                print(f"{e}; fix that and play the clip again, or q to stop")
                break


def run(
    trials: Sequence[Trial],
    play: Callable[[np.ndarray], None] | None = None,
    ask: Callable[[str], str] = input,
) -> list[Answer]:
    """Take a listener through the trials at the console. play gets each clip at 8 kHz (by
    default it goes to the default sound card) and ask reads a line. Returns the answers
    given, fewer than the trials if the listener stops early with q."""
    play = play or speaker()
    print(
        "In each trial A and B are the same speech, one with data under it and one without,\n"
        "and X is one of the two. Type a, b or x and Enter to hear that clip, as often as you\n"
        "like (axbx plays four in a row), then 1 if X is A or 2 if X is B. q stops early and\n"
        "keeps the answers so far. Headphones in a quiet room work best."
    )
    answers = []
    for i, trial in enumerate(trials, 1):
        started = time.monotonic()
        said = _listen(trial, f"trial {i} of {len(trials)}: ", play, ask)
        if said is None:
            break
        seconds = round(time.monotonic() - started, 1)
        answers.append(
            Answer(trial.level_db, i, trial.sentence, trial.data_was, trial.x_was, said, seconds)
        )
    return answers


def report(answers: Sequence[Answer]) -> str:
    """Per level: answers, how many were right, and the chance of doing that well or better
    by guessing (exact one-sided binomial test). Heard means under 5% over 8 answers or more."""
    if not answers:
        return "no answers"
    rows = [f"{'level_db':>8}  {'answers':>7}  {'correct':>7}  {'percent':>7}  {'p':>8}  verdict"]
    lines = []
    for level in sorted({a.level_db for a in answers}):
        right = [a.correct for a in answers if a.level_db == level]
        n, k = len(right), sum(right)
        p = binomtest(k, n, alternative="greater").pvalue
        verdict = "too few" if n < 8 else "heard" if p < 0.05 else "not heard"
        rows.append(f"{level:8.1f}  {n:7d}  {k:7d}  {100 * k / n:7.1f}  {p:8.3g}  {verdict}")
        lines.append(
            f"undervoice ABX at {level:g} dB: {k} of {n} correct ({100 * k / n:.0f}%), "
            f"p = {p:.3g}, {verdict}"
        )
    return "\n".join([*rows, "", "To share the result, paste these lines into an issue:", *lines])


def save(answers: Iterable[Answer], path: str | Path) -> None:
    """Append answers to a results CSV, starting it with a header if it is new, so runs by
    several people can go in one file."""
    path = Path(path)
    new = not path.exists() or path.stat().st_size == 0
    if not new:
        with open(path, newline="", encoding="utf-8") as f:
            if next(csv.reader(f), None) != COLUMNS:
                raise ValueError(f"{path} is not an ABX results file, so not adding to it")
    with open(path, "a", newline="", encoding="utf-8") as f:
        out = csv.DictWriter(f, COLUMNS)
        if new:
            out.writeheader()
        out.writerows(asdict(a) | {"correct": int(a.correct)} for a in answers)


def load(paths: Iterable[str | Path]) -> list[Answer]:
    """The answers in one or more results files written by save(), in order."""
    answers = []
    for path in paths:
        with open(path, newline="", encoding="utf-8") as f:
            rows = csv.DictReader(f)
            if rows.fieldnames != COLUMNS:
                raise ValueError(
                    f"{path} is not an ABX results file: it does not start with {','.join(COLUMNS)}"
                )
            for row in rows:
                where = f"{path} line {rows.line_num}"
                if not {row["data_was"], row["x_was"], row["answer"]} <= {"A", "B"}:
                    raise ValueError(f"{where}: data_was, x_was and answer must each be A or B")
                try:
                    level, trial = float(row["level_db"]), int(row["trial"])
                    seconds = float(row["seconds"])
                except (TypeError, ValueError) as e:
                    raise ValueError(f"{where}: {e}") from e
                answers.append(
                    Answer(
                        level,
                        trial,
                        row["sentence"],
                        row["data_was"],
                        row["x_was"],
                        row["answer"],
                        seconds,
                    )
                )
    return answers


def export(trials: Sequence[Trial], folder: str | Path) -> None:
    """Write each trial's clips as 8 kHz WAVs (001_A.wav, 001_B.wav, 001_X.wav and on) and
    key.csv, which says which clip has the data and which one X is. Keep the key from the
    listener until the end."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "key.csv", "w", newline="", encoding="utf-8") as f:
        key = csv.writer(f)
        key.writerow(["level_db", "trial", "sentence", "data_was", "x_was"])
        for i, t in enumerate(trials, 1):
            for name, clip in (("A", t.a), ("B", t.b), ("X", t.x)):
                write_wav(folder / f"{i:03d}_{name}.wav", clip)
            key.writerow([t.level_db, i, t.sentence, t.data_was, t.x_was])
