"""Hide a slow data channel under FM voice: encode, decode, simulate and run live."""

from __future__ import annotations

import argparse
import glob
import random
import sys
import time
from contextlib import AbstractContextManager, nullcontext

import numpy as np

from . import _abx, _check, _live
from ._audio import read_wav, write_wav
from ._channel import fm_channel
from ._message import MAX_PARTS, PART_LEN, Message, Part, split
from ._messages import Inbox, heard_line
from ._modes import MODES, VOICE, decode, encode, frame_length
from ._modulator import LEVEL_DB, SHAPE
from ._ptt import Keyer, SetupError, open_keyer
from ._sweep import sweep
from ._updater import current
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


def _paths(patterns: list[str]) -> list[str]:
    # PowerShell hands patterns like speech/*.wav over unexpanded
    return [p for pattern in patterns for p in sorted(glob.glob(pattern)) or [pattern]]


def _read(path: str) -> np.ndarray:
    try:
        return read_wav(path)
    except OSError as e:
        sys.exit(f"can't read {path}: {e.strerror or e}")
    except ValueError as e:
        sys.exit(f"can't read {path}: {e}")


def _check_pause(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    pause = getattr(args, "pause_level", None)
    if pause is None or getattr(args, "mode", VOICE) != VOICE:
        return
    level = min(args.levels) if hasattr(args, "levels") else args.level
    if pause > level:
        parser.error(f"--pause-level ({pause:g} dB) can't be above the data level ({level:g} dB)")


def _parts(parser: argparse.ArgumentParser, args: argparse.Namespace) -> list[Part]:
    text, to = getattr(args, "msg", None), getattr(args, "to", None)
    if text is None:
        if to is not None:
            parser.error("--to needs --msg, the message to send")
        return []
    try:
        return split(args.call, to or "", text, random.randrange(64))
    except ValueError as e:
        parser.error(str(e))


def _check_setup(args: argparse.Namespace) -> None:
    message = _message(args)
    print(f"recording {_check.SECONDS:.0f} s from the microphone: talk normally")
    mic = _check.MicCheck(args.input)
    while not mic.done:
        time.sleep(1)
        print(f"  {mic.left:3.0f} s left, peak {20 * np.log10(max(mic.peak, 1e-6)):6.1f} dBFS")
    result = _check.finish(
        mic.audio(), message, args.level, args.shape, args.pause_level, args.mode
    )
    print(f"peaks {result.peak_db:.1f} dBFS, speech about {result.speech_db:.1f} dBFS")
    print(result.advice)
    if result.frames:
        print(f"the data mixed into it decodes: {message}")
    else:
        print("the data mixed into it did not decode; talk for the whole recording and try again")
    print("playing back what would go to the radio, on the default output")
    _check.play_back(result.mixed, wait=True)
    if args.ptt:
        with open_keyer(args.ptt, args.ptt_gpio) as keyer:
            print(f"keying the radio for {_check.PTT_SECONDS:.0f} s through {keyer.name}")
            _check.key_briefly(keyer)
        print("unkeyed")


def _abx_session(args: argparse.Namespace) -> None:
    try:
        if args.score:
            print(_abx.report(_abx.load(_paths(args.score))))
            return
        if not args.speech:
            sys.exit("give WAV files of speech to build the trials from, or --score results files")
        if args.save:
            # before the test, so a wrong file does not cost the listener their answers
            _abx.check_results(args.save)
        speech = [_read(path) for path in _paths(args.speech)]
        pauses = "" if args.pause_level is None else f", {args.pause_level:g} dB in pauses"
        print(
            f"building {args.trials} trials per level, shape {args.shape}{pauses}, "
            f"{args.snr:g} dB link"
        )
        rng = np.random.default_rng(args.seed)
        trials = _abx.make_trials(
            speech, args.levels, args.shape, args.snr, args.trials, rng, args.pause_level
        )
        if args.export:
            _abx.export(trials, args.export)
            print(f"wrote {len(trials)} trials and key.csv to {args.export}")
            return
        answers = _abx.run(trials, _abx.speaker(args.output))
        print(_abx.report(answers))
        if args.save:
            _abx.save(answers, args.save)
            print(
                f"added {len(answers)} answers to {args.save}; keep one file per shape, SNR "
                "and pause level"
            )
    except ValueError as e:
        sys.exit(str(e))


def _message(args: argparse.Namespace) -> Message:
    try:
        return Message(args.call, args.grid, args.status)
    except ValueError as e:
        sys.exit(str(e))


def _add_message(p: argparse.ArgumentParser) -> None:
    p.add_argument("--call", required=True, help="callsign, up to 10 characters")
    p.add_argument("--grid", default="", help="4 or 6 character Maidenhead locator")
    p.add_argument("--status", default="", help="up to 32 characters of A-Z 0-9 / - . and space")


def _add_text(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--to", metavar="CALL", help="callsign the --msg is for; leave out to send it to everyone"
    )
    p.add_argument(
        "--msg",
        metavar="TEXT",
        help=f"text message to send ahead of the station frame: up to {PART_LEN * MAX_PARTS} "
        f"characters of A-Z 0-9 / - . ? , ! ' : and space, {PART_LEN} to a frame",
    )


def _add_mode(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--mode",
        choices=MODES,
        default=VOICE,
        help="voice: data under the speech, works through repeaters (default); sub: "
        "sub-audible below 300 Hz through a 9600 data port, simplex and tone-free channels only, "
        "which ignores --shape",
    )


def _add_tuning(p: argparse.ArgumentParser, level: bool = True) -> None:
    if level:
        p.add_argument(
            "--level",
            type=float,
            default=LEVEL_DB,
            help=f"data level in dB relative to the speech, or to full scale in sub mode "
            f"(default {LEVEL_DB})",
        )
        p.add_argument(
            "--pause-level",
            type=float,
            help="keep the data going through pauses at this many dB below the recent speech, "
            "instead of stopping; no higher than --level, and ignored in sub mode",
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
    parser.add_argument("--version", action="version", version=f"undervoice {current()}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("encode", help="mix data under the speech in a WAV file")
    p.add_argument("input")
    p.add_argument("output")
    _add_message(p)
    _add_text(p)
    _add_tuning(p)
    _add_mode(p)

    p = sub.add_parser("decode", help="print the frames found in a WAV file")
    p.add_argument("input")
    _add_tuning(p, level=False)
    _add_mode(p)

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
    p.add_argument(
        "--path",
        choices=("voice", "flat"),
        default="voice",
        help="voice: mic and speaker audio with their filters and emphasis (default); flat: "
        "a 9600 data port, as the sub-audible mode needs",
    )
    p.add_argument("--seed", type=int)

    p = sub.add_parser("sweep", help="frame loss against data level through the FM channel")
    p.add_argument("speech", nargs="+", help="WAV files of speech")
    p.add_argument("--levels", type=level_list, default=level_list("-36,-30,-24,-18,-12"))
    p.add_argument("--trials", type=int, default=3)
    p.add_argument("--snr", type=float, default=40.0)
    p.add_argument("--hops", type=int, default=1)
    p.add_argument("--ppm", type=float, default=0.0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument(
        "--pause-level",
        type=float,
        help="keep data going through pauses at this many dB below recent speech, no higher "
        "than the lowest of --levels",
    )
    _add_tuning(p, level=False)

    p = sub.add_parser("tx", help="send data under live mic audio, or under a WAV file")
    _add_message(p)
    _add_text(p)
    _add_tuning(p)
    _add_mode(p)
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
    p.add_argument(
        "--cw-id",
        action="store_true",
        help="send your callsign in Morse before unkeying: at the end of a --play recording; "
        "with the mic, at the end of the first over and of any that ends 9 minutes or more "
        "after the last ID (without --ptt the whole run is one over)",
    )
    p.add_argument(
        "--messages",
        metavar="FILE",
        help="add the --msg to this text file once all of it has gone out",
    )

    p = sub.add_parser("rx", help="listen on a sound card and print frames")
    _add_tuning(p, level=False)
    _add_mode(p)
    p.add_argument("--input", type=_device, help="radio audio device (number or name)")
    p.add_argument("--record", metavar="WAV", help="also save the received audio to this file")
    p.add_argument(
        "--messages",
        metavar="FILE",
        help="add each message heard to this text file, as it completes or, if parts are "
        "missing, as it stands when rx stops",
    )

    p = sub.add_parser("abx", help="blind listening test: can you hear the data?")
    p.add_argument("speech", nargs="*", help="WAV files of speech to build the trials from")
    p.add_argument("--levels", type=level_list, default=level_list("-24,-18,-12"))
    p.add_argument("--trials", type=int, default=10, help="trials per level (default 10)")
    p.add_argument("--snr", type=float, default=40.0, help="simulated FM link SNR (default 40)")
    p.add_argument("--seed", type=int, help="fixes the trial order; leave out for a fresh one")
    p.add_argument("--output", type=_device, help="sound device to play on (number or name)")
    p.add_argument("--save", metavar="CSV", help="append the answers to this results file")
    p.add_argument("--export", metavar="FOLDER", help="write the clips and answer key instead")
    p.add_argument("--score", nargs="+", metavar="CSV", help="report on saved results instead")
    p.add_argument(
        "--pause-level",
        type=float,
        help="keep the data going through pauses at this many dB below the recent speech, no "
        "higher than the lowest of --levels",
    )
    _add_tuning(p, level=False)

    p = sub.add_parser("check", help="test the microphone, and optionally PTT, before going on air")
    _add_message(p)
    _add_tuning(p)
    _add_mode(p)
    p.add_argument("--input", type=_device, help="microphone (number or name)")
    p.add_argument(
        "--ptt",
        nargs="?",
        const="cm108",
        metavar="HOW",
        help="afterwards key the radio for 2 seconds this way (the same values tx takes)",
    )
    p.add_argument("--ptt-gpio", type=int, default=3, help="CM108 GPIO pin wired to PTT")

    sub.add_parser("devices", help="list sound devices")
    sub.add_parser("gui", help="open the undervoice window")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    _check_pause(parser, args)
    args.parts = _parts(parser, args)
    try:
        return _run(args)
    except SetupError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        return 130


def _run(args: argparse.Namespace) -> int:
    if args.command == "encode":
        recording = _read(args.input)
        message = _message(args)
        mixed = encode(
            args.mode, recording, message, args.level, args.shape, args.pause_level, args.parts
        )
        write_wav(args.output, mixed)
    elif args.command == "decode":
        frames = decode(args.mode, _read(args.input), args.shape)
        inbox = Inbox()
        for f in frames:
            print(f"{f.start / RATE:8.2f} s  {f.message}  (snr {f.snr_db:.1f} dB)")
            if isinstance(f.message, Part):
                m = inbox.add(f.message)
                if m is not None and m.complete:
                    print(f"message: {heard_line(m)}")
        for m in inbox.messages:
            if not m.complete:
                print(f"message: {heard_line(m)}")
        if not frames:
            print("no frames decoded")
            return 1
    elif args.command == "channel":
        rng = np.random.default_rng(args.seed)
        audio = _read(args.input)
        out = fm_channel(audio, args.snr, args.drive, args.hops, args.ppm, rng, path=args.path)
        write_wav(args.output, out)
    elif args.command == "sweep":
        speech = [_read(path) for path in _paths(args.speech)]
        message = Message("N0CALL", "FN31pr", "SWEEP TEST 0123456789 ABCDEFGHIJ")
        print(f"shape {args.shape}, snr {args.snr} dB, hops {args.hops}, ppm {args.ppm}")
        print("level_db  frames  received  loss_pct  loss_95_upper_pct  wrong")
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
            args.pause_level,
        ):
            print(
                f"{r.level_db:8.1f}  {r.sent:6d}  {r.received:8d}  {100 * r.loss:8.1f}  "
                f"{100 * r.loss_upper:17.2f}  {r.wrong:5d}",
                flush=True,
            )
    elif args.command == "tx":
        message = _message(args)
        if not args.cw_id:
            print(
                "give your callsign by voice every 10 minutes and at the end "
                "(97.119), or add --cw-id to send it in Morse",
                file=sys.stderr,
            )
        with _keyer(args) as ptt:
            if args.play:
                recording = _read(args.play)
                print(f"playing {len(recording) / RATE:.1f} s with {message}")
                left_out = args.parts[len(recording) // frame_length(args.mode) :]
                if left_out:
                    noun = "part" if len(left_out) == 1 else "parts"
                    numbers = ", ".join(str(p.index + 1) for p in left_out)
                    print(
                        f"{args.play} is too short for the whole message: {noun} {numbers} "
                        f"of {len(args.parts)} won't go out",
                        file=sys.stderr,
                    )
                _live.play(
                    recording,
                    message,
                    args.level,
                    args.shape,
                    args.output,
                    ptt,
                    args.txdelay,
                    args.pause_level,
                    args.cw_id,
                    args.mode,
                    args.parts,
                )
                if args.parts and not left_out:
                    _live.log_sent(args.messages, args.parts)
            else:
                _live.transmit(
                    message,
                    args.level,
                    args.shape,
                    args.input,
                    args.output,
                    ptt,
                    args.txdelay,
                    args.pause_level,
                    args.cw_id,
                    args.mode,
                    args.parts,
                    args.messages,
                )
    elif args.command == "rx":
        _live.receive(args.shape, args.input, args.record, args.mode, args.messages)
    elif args.command == "abx":
        _abx_session(args)
    elif args.command == "check":
        _check_setup(args)
    elif args.command == "devices":
        _live.devices()
    elif args.command == "gui":
        # some minimal Linux Pythons ship without tkinter, so only this command needs it
        from ._gui import run

        run()
    return 0
