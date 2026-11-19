import numpy as np

from undervoice._fec import decode_bits, encode_bits

DATA = bytes(range(34))


def test_corrects_noisy_soft_values():
    rng = np.random.default_rng(3)
    coded = encode_bits(DATA)
    soft = 1.0 - 2.0 * coded + rng.normal(0, 0.7, len(coded))
    assert np.any(np.sign(soft) != 1.0 - 2.0 * coded)
    assert decode_bits(soft) == DATA


def test_survives_a_long_burst_of_erasures():
    # a pause in speech wipes out a run of consecutive symbols on air
    soft = 1.0 - 2.0 * encode_bits(DATA)
    soft[200:350] = 0.0
    assert decode_bits(soft) == DATA


def test_too_much_noise_does_not_decode():
    rng = np.random.default_rng(4)
    soft = 1.0 - 2.0 * encode_bits(DATA) + rng.normal(0, 3.0, 558)
    assert decode_bits(soft) != DATA
