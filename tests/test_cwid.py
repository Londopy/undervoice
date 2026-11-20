import time

import numpy as np
import pytest

from undervoice._cwid import CODE, due, morse
from undervoice._waveform import RATE

UNIT = int(RATE * 1.2 / 20)


def read_back(audio):
    """Decode keyed audio to text from its on/off timing, as a listener would."""
    keyed = np.abs(audio) > 0.05
    # smooth over the tone's own zero crossings, a few ms each
    keyed = np.convolve(keyed, np.ones(40), mode="same") > 0
    runs, state, length = [], keyed[0], 0
    for k in keyed:
        if k == state:
            length += 1
        else:
            runs.append((state, length))
            state, length = k, 1
    runs.append((state, length))
    lookup = {v: k for k, v in CODE.items()}
    text, letter = "", ""
    for on, n in runs:
        units = round(n / UNIT)
        if on:
            letter += "." if units == 1 else "-"
        elif units >= 3:
            text += lookup[letter]
            letter = ""
            if units >= 7:
                text += " "
    return text + (lookup[letter] if letter else "")


@pytest.mark.parametrize("call", ["W1AW", "VE3/K1ABC", "N0CALL", "W1AW 7"])
def test_morse_reads_back_as_the_callsign(call):
    assert read_back(morse(call)) == call


def test_morse_timing_is_20_wpm():
    # W1AW is 49 units of characters and gaps plus 3 units of trailing space
    assert len(morse("W1AW")) == 52 * UNIT
    # a space is a 7 unit word gap, then 7 is 13 units
    assert len(morse("W1AW 7")) == (49 + 7 + 13 + 3) * UNIT


def test_unknown_character_is_reported():
    with pytest.raises(ValueError, match="no Morse"):
        morse("W1AW#")


def test_id_falls_due_a_minute_before_the_ten_minute_limit():
    now = time.monotonic()
    assert due(None)
    assert not due(now - 8.9 * 60)
    assert due(now - 9 * 60)
