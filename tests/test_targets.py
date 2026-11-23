"""The spec's targets, measured through the simulated FM channel. Slow: run with -m slow."""

from pathlib import Path

import pytest

from undervoice import Message, read_wav
from undervoice._modulator import LEVEL_DB, SHAPE
from undervoice._sweep import sweep

SPEECH = sorted((Path(__file__).parent.parent / "speech").glob("*.wav"))


@pytest.mark.slow
@pytest.mark.skipif(not SPEECH, reason="speech recordings not present")
def test_under_one_percent_frame_loss_on_a_clean_simplex_path():
    speech = [read_wav(p) for p in SPEECH]
    message = Message("N0CALL", "FN31pr", "TARGET CHECK 0123456789 ABCDEFG")
    [result] = sweep(speech, [LEVEL_DB], message, SHAPE, 40, 40.0, 1, 0.0, seed=1)
    assert result.sent >= 480
    # the README's "480 frames out of 480"
    assert result.received == result.sent
    assert result.wrong == 0
    # 95% confident the true loss rate is below 1%, not just that none were lost this time
    assert result.loss_upper < 0.01
