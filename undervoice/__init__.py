"""A slow data channel hidden under FM voice.

Basic use::

    from undervoice import Message, decode, encode, read_wav

    speech = read_wav("speech.wav")
    mixed = encode(speech, Message("W1AW", "FN31pr", "ON THE AIR"))
    for frame in decode(mixed):
        print(frame.message)
"""

from ._audio import read_wav, write_wav
from ._channel import fm_channel
from ._demodulator import Frame, decode
from ._message import Message, Part, split
from ._modulator import Modulator, encode
from ._waveform import FRAME, RATE

__all__ = [
    "FRAME",
    "RATE",
    "Frame",
    "Message",
    "Modulator",
    "Part",
    "decode",
    "encode",
    "fm_channel",
    "read_wav",
    "split",
    "write_wav",
]
