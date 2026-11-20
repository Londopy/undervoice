"""Station identification in Morse, which 97.119(b)(1) accepts in place of a spoken callsign."""

from __future__ import annotations

import numpy as np

from ._waveform import RATE

# 97.119 asks for an ID at least every 10 minutes during a communication
ID_EVERY = 600.0
WPM = 20
TONE = 800.0
LEVEL = 0.3

CODE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.", "G": "--.",
    "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..", "M": "--", "N": "-.",
    "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-", "U": "..-",
    "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--", "Z": "--..", "0": "-----",
    "1": ".----", "2": "..---", "3": "...--", "4": "....-", "5": ".....", "6": "-....",
    "7": "--...", "8": "---..", "9": "----.", "/": "-..-.", "-": "-....-", ".": ".-.-.-",
}  # fmt: skip


def morse(call: str, wpm: float = WPM, tone: float = TONE, level: float = LEVEL) -> np.ndarray:
    """The callsign keyed at wpm (PARIS timing) as RATE audio, with 5 ms edges so it does
    not click."""
    unit = int(RATE * 1.2 / wpm)
    t = np.arange(unit * 3) / RATE
    edge = int(0.005 * RATE)
    ramp = np.ones(unit * 3)
    ramp[:edge] = np.linspace(0, 1, edge)
    pieces: list[np.ndarray] = []

    def on(units: int) -> None:
        n = unit * units
        shaped = ramp[:n].copy()
        shaped[-edge:] = np.linspace(1, 0, edge)
        pieces.append(level * np.sin(2 * np.pi * tone * t[:n]) * shaped)

    def off(units: int) -> None:
        pieces.append(np.zeros(unit * units))

    for i, ch in enumerate(call.upper()):
        if ch not in CODE:
            raise ValueError(f"no Morse for {ch!r} in callsign {call!r}")
        if i:
            off(3)
        for j, mark in enumerate(CODE[ch]):
            if j:
                off(1)
            on(1 if mark == "." else 3)
    off(3)
    return np.concatenate(pieces)
