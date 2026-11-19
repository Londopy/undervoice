"""Hide a slow data channel under FM voice: encode, decode, simulate and run live."""

from __future__ import annotations

import argparse
import glob
import sys
from contextlib import AbstractContextManager, nullcontext

import numpy as np

from . import _live
from ._audio import read_wav, write_wav
from ._channel import fm_channel
from ._demodulator import decode
from ._message import Message
from ._modulator import LEVEL_DB, SHAPE, encode
from ._ptt import Keyer, SetupError, open_keyer
from ._sweep import sweep
from ._waveform import RATE


def _device(value: str) -> int | str:
    return int(value) if value.isdigit() else value


def shape_value(value: str) -> float:
    shape = float(value)
    if not 0.0 <= shape < 1.0:
        raise argparse.ArgumentTypeError(
            f"shape must be from 0 up to (not including) 1, got {value}"
        )
    return shape


def level_list(value: str) -> list[float]:
    return [float(v) for v in value.split(",")]


def _keyer(args: argparse.Namespace) -> AbstractContextManager[Keyer | None]:
    if args.ptt is None:
        return nullcontext(None)
    return open_keyer(args.ptt, args.ptt_gpio)


def _message(args: argparse.Namespace) -> Message:
    try:
        return Message(args.call, args.grid, args.status)
    except ValueError as e:
        sys.exit(str(e))


def _add_message(p: argparse.ArgumentParser) -> None:
    p.add_argument("--call", required=True, help="callsign, up to 10 characters")
    p.add_argument("--grid", default="", help="4 or 6 character Maidenhead locator")
    p.add_argument("--status", default="", help="up to 32 characters of A-Z 0-9 / - . and space")


def _add_tuning(p: argparse.ArgumentParser, level: bool = True) -> None:
    if level:
        p.add_argument(
            "--level",
            type=float,
            default=LEVEL_DB,
            help=f"data level in dB relative to the speech (default {LEVEL_DB})",
        )
    p.add_argument(
        "--shape",
        type=shape_value,
        default=SHAPE,
        help=f"how closely the data follows the speech spectrum, 0 to below 1 "
        f"(default {SHAPE}); transmitter and receiver must match",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="undervoice", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("encode", help="mix data under the speech in a WAV file")
    p.add_argument("input")
    p.add_argument("output")
    _add_message(p)
    _add_tuning(p)

    p = sub.add_parser("decode", help="print the frames found in a WAV file")
    p.add_argument("input")
    _add_tuning(p, level=False)

    p = sub.add_parser("channel", help="pass a WAV file through the simulated FM channel")
    p.add_argument("input")
    p.add_argument("output")
    p.add_argument(
        "--snr",
        type=float,
        default=40.0,
        help="full-deviation 1 kHz tone to noise ratio at the receiver, dB (default 40)",
    )
    p.add_argument(
        "--drive",
        type=float,
        default=3.0,
        help="how far speech peaks push into the deviation limiter, dB (default 3)",
    )
    p.add_argument("--hops", type=int, default=1, help="1 for simplex, 2 through a repeater")
    p.add_argument("--ppm", type=float, default=0.0, help="sound card clock offset")
    p.add_argument("--seed", type=int)

    p = sub.add_parser("sweep", help="frame loss against data level through the FM channel")
    p.add_argument("speech", nargs="+", help="WAV files of speech")
    p.add_argument("--levels", type=level_list, default=level_list("-36,-30,-24,-18,-12"))
    p.add_argument("--trials", type=int, default=3)
    p.add_argument("--snr", type=float, default=40.0)
    p.add_argument("--hops", type=int, default=1)
    p.add_argument("--ppm", type=float, default=0.0)
    p.add_argument("--seed", type=int, default=0)
    _add_tuning(p, level=False)

    p = sub.add_parser("tx", help="send data under live mic audio, or under a WAV file")
    _add_message(p)
    _add_tuning(p)
    p.add_argument("--play", metavar="WAV", help="play this recording instead of using the mic")
    p.add_argument("--input", type=_device, help="mic device (number or name)")
    p.add_argument("--output", type=_device, help="radio audio device (number or name)")
    p.add_argument(
        "--ptt",
        nargs="?",
        const="cm108",
        metavar="HOW",
        help="key the radio: cm108[:N] for a USB interface's GPIO (the default), COM5 or "
        "/dev/ttyUSB0 with optional :dtr for a serial line, rigctld[:host[:port]] for CAT "
        "through Hamlib; leave out for VOX or keying by hand. `undervoice devices` lists them",
    )
    p.add_argument(
        "--ptt-gpio", type=int, default=3, help="CM108 GPIO pin wired to PTT (default 3)"
    )
    p.add_argument(
        "--txdelay",
        type=float,
        default=0.3,
        help="seconds between keying and sending audio (default 0.3)",
    )

    p = sub.add_parser("rx", help="listen on a sound card and print frames")
    _add_tuning(p, level=False)
    p.add_argument("--input", type=_device, help="radio audio device (number or name)")
    p.add_argument("--record", metavar="WAV", help="also save the received audio to this file")

    sub.add_parser("devices", help="list sound devices")
    sub.add_parser("gui", help="open the undervoice window")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return _run(args)
    except SetupError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        return 130


def _run(args: argparse.Namespace) -> int:
    if args.command == "encode":
        write_wav(args.output, encode(read_wav(args.input), _message(args), args.level, args.shape))
    elif args.command == "decode":
        frames = decode(read_wav(args.input), args.shape)
        for f in frames:
            print(f"{f.start / RATE:8.2f} s  {f.message}  (snr {f.snr_db:.1f} dB)")
        if not frames:
            print("no frames decoded")
            return 1
    elif args.command == "channel":
        rng = np.random.default_rng(args.seed)
        out = fm_channel(read_wav(args.input), args.snr, args.drive, args.hops, args.ppm, rng)
        write_wav(args.output, out)
    elif args.command == "sweep":
        # PowerShell hands patterns like speech/*.wav over unexpanded
        paths = [p for pattern in args.speech for p in sorted(glob.glob(pattern)) or [pattern]]
        speech = [read_wav(path) for path in paths]
        message = Message("N0CALL", "FN31pr", "SWEEP TEST 0123456789 ABCDEFGHIJ")
        print(f"shape {args.shape}, snr {args.snr} dB, hops {args.hops}, ppm {args.ppm}")
        print("level_db  frames  received  loss_pct  wrong")
        for r in sweep(
            speech,
            args.levels,
            message,
            args.shape,
            args.trials,
            args.snr,
            args.hops,
            args.ppm,
            args.seed,
        ):
            print(
                f"{r.level_db:8.1f}  {r.sent:6d}  {r.received:8d}  {100 * r.loss:8.1f}  {r.wrong:5d}",
                flush=True,
            )
    elif args.command == "tx":
        message = _message(args)
        with _keyer(args) as ptt:
            if args.play:
                speech = read_wav(args.play)
                print(f"playing {len(speech) / RATE:.1f} s with {message}")
                _live.play(speech, message, args.level, args.shape, args.output, ptt, args.txdelay)
            else:
                _live.transmit(
                    message, args.level, args.shape, args.input, args.output, ptt, args.txdelay
                )
    elif args.command == "rx":
        _live.receive(args.shape, args.input, args.record)
    elif args.command == "devices":
        _live.devices()
    elif args.command == "gui":
        # some minimal Linux Pythons ship without tkinter, so only this command needs it
        from ._gui import run

        run()
    return 0
