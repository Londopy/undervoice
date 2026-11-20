from pathlib import Path

import pytest

from undervoice._cli import main
from undervoice._sweep import Result

SPEECH = Path(__file__).parent.parent / "speech" / "OSR_us_000_0010_8k.wav"


def test_no_loss_in_300_frames_rules_out_about_1_percent():
    # with none lost out of n, the one-sided 95% bound p solves (1 - p)**n = 0.05
    assert Result(-18.0, 300, 300, 0).loss_upper == pytest.approx(1 - 0.05 ** (1 / 300))
    # one lost moves it to 1.57%, the exact Clopper-Pearson bound for 1 in 300
    assert Result(-18.0, 300, 299, 0).loss_upper == pytest.approx(0.01571, abs=1e-5)


def test_nothing_received_rules_out_nothing():
    assert Result(-18.0, 10, 0, 0).loss_upper == 1.0


@pytest.mark.skipif(not SPEECH.exists(), reason="speech recordings not present")
def test_sweep_keeps_the_data_going_through_pauses_when_asked(capsys):
    def received(*pause):
        args = ["--levels=-21", "--trials", "1", "--snr", "30", "--seed", "2", *pause]
        assert main(["sweep", str(SPEECH), *args]) == 0
        return int(capsys.readouterr().out.splitlines()[-1].split()[2])

    # on a noisy path the frames spanning this recording's pauses are lost without it
    assert received() == 0
    assert received("--pause-level", "-28") == 3
