import numpy as np

from undervoice._audio import Decimator, Interpolator, read_wav, resample_at
from undervoice._live import _recorder


def _in_blocks(convert, x, sizes):
    out, i = [], 0
    for n in sizes:
        out.append(convert(x[i : i + n]))
        i += n
    return np.concatenate(out)


def test_converters_do_not_depend_on_block_size():
    rng = np.random.default_rng(5)
    x = rng.standard_normal(960 * 20)
    np.testing.assert_allclose(_in_blocks(Decimator(), x, [960] * 20), Decimator()(x), atol=1e-12)
    y = rng.standard_normal(3000)
    sizes = [160, 7, 1, 832, 2000]
    np.testing.assert_allclose(_in_blocks(Interpolator(), y, sizes), Interpolator()(y), atol=1e-12)


def test_round_trip_through_card_rate_keeps_voice_band():
    t = np.arange(8000) / 8000
    tone = np.sin(2 * np.pi * 1000 * t)
    back = Decimator()(Interpolator()(tone))
    # the two FIR filters together delay by 20 samples at 8 kHz
    np.testing.assert_allclose(back[1000:7000], tone[980:6980], atol=0.02)


def test_resample_at_fractional_times():
    t = np.arange(4000)
    x = np.sin(2 * np.pi * 700 * t / 8000)
    when = np.linspace(100, 3800, 999)
    np.testing.assert_allclose(
        resample_at(x, when), np.sin(2 * np.pi * 700 * when / 8000), atol=1e-3
    )


def test_recorder_writes_what_it_is_given(tmp_path):
    path = tmp_path / "rx.wav"
    blocks = [np.full(160, 0.25), np.full(160, -0.5)]
    with _recorder(str(path)) as save:
        for b in blocks:
            save(b)
    np.testing.assert_allclose(read_wav(path), np.concatenate(blocks), atol=1e-4)
