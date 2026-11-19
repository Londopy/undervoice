"""Keying the radio: a CM108-family GPIO pin, a serial port's RTS or DTR line, or a CAT
command through Hamlib's rigctld. VOX interfaces such as the SignaLink need none of these."""

from __future__ import annotations

import socket
from abc import ABC, abstractmethod
from types import ModuleType, TracebackType
from typing import Any

# C-Media and SSS make the sound chips in most USB radio interfaces (DMK URI, RIM-Lite and
# others); 1209:7388 is the AIOC cable, which copies the CM108 protocol
VENDORS = {0x0D8C, 0x0C76}
AIOC = (0x1209, 0x7388)
RIGCTLD_PORT = 4532


class SetupError(Exception):
    """A sound card or PTT interface can't be used as asked; the message says why."""


class Keyer(ABC):
    """Something that keys the radio. Leaving a with block unkeys it and lets the interface
    go, so Ctrl+C or an error never leaves the radio transmitting."""

    name: str

    @abstractmethod
    def key(self, on: bool) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> Keyer:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()


def _hid() -> ModuleType:
    try:
        import hid
    except ImportError as e:
        raise SetupError("USB interface PTT needs hidapi: pip install undervoice[live]") from e
    return hid


def _serial() -> ModuleType:
    try:
        import serial
    except ImportError as e:
        raise SetupError("serial port PTT needs pyserial: pip install undervoice[live]") from e
    return serial


def find() -> list[dict[str, Any]]:
    """HID entries for every attached CM108-type interface."""
    return [
        d
        for d in _hid().enumerate()
        if d["vendor_id"] in VENDORS or (d["vendor_id"], d["product_id"]) == AIOC
    ]


def describe(d: dict[str, Any]) -> str:
    name = d["product_string"] or d["manufacturer_string"] or "USB audio interface"
    return f"{name} ({d['vendor_id']:04x}:{d['product_id']:04x})"


class Cm108(Keyer):
    """PTT on a GPIO pin of a CM108-family USB sound card."""

    def __init__(self, choice: int | None = None, gpio: int = 3):
        found = find()
        if not found:
            raise SetupError("no CM108-type USB radio interface found for PTT")
        if choice is None and len(found) > 1:
            names = "; ".join(f"cm108:{i} {describe(d)}" for i, d in enumerate(found))
            raise SetupError(f"more than one USB interface can key PTT, pick one: {names}")
        index = choice or 0
        if index >= len(found):
            raise SetupError(f"no USB PTT interface number {index}; undervoice devices lists them")
        self.name = describe(found[index])
        self.mask = 1 << (gpio - 1)
        self.dev = _hid().device()
        try:
            self.dev.open_path(found[index]["path"])
        except OSError as e:
            raise SetupError(f"can't open {self.name} for PTT: {e}") from e

    def key(self, on: bool) -> None:
        # report 0, then the CM108's four output bytes: reserved, GPIO direction, GPIO level,
        # reserved; the pin is driven as an output and set high to key
        self.dev.write([0, 0, self.mask, self.mask if on else 0, 0])

    def close(self) -> None:
        self.key(False)
        self.dev.close()


class SerialLine(Keyer):
    """PTT on a serial port's RTS or DTR line (Digirig and most interfaces with a COM port)."""

    def __init__(self, port: str, line: str = "rts"):
        serial = _serial()
        self.name = f"{port} {line.upper()}"
        self.line = line
        self.port = serial.Serial()
        self.port.port = port
        # set before opening: otherwise Windows raises both lines on open and keys the radio
        self.port.rts = False
        self.port.dtr = False
        try:
            self.port.open()
        except serial.SerialException as e:
            raise SetupError(f"can't open {port} for PTT: {e}") from e

    def key(self, on: bool) -> None:
        setattr(self.port, self.line, on)

    def close(self) -> None:
        self.key(False)
        self.port.close()


class Rigctld(Keyer):
    """PTT by CAT command through a running Hamlib rigctld, for any rig Hamlib supports."""

    def __init__(self, host: str = "localhost", port: int = RIGCTLD_PORT):
        self.name = f"rigctld at {host}:{port}"
        try:
            self.sock = socket.create_connection((host, port), timeout=3)
        except OSError as e:
            raise SetupError(f"can't reach rigctld at {host}:{port}: {e}; is it running?") from e
        self.replies = self.sock.makefile("r", encoding="ascii")

    def key(self, on: bool) -> None:
        try:
            self.sock.sendall(b"T 1\n" if on else b"T 0\n")
            reply = self.replies.readline().strip()
        except OSError as e:
            raise SetupError(f"lost rigctld while keying: {e}") from e
        if reply != "RPRT 0":
            raise SetupError(f"rigctld refused PTT: {reply or 'no reply'}")

    def close(self) -> None:
        try:
            self.key(False)
        finally:
            self.sock.close()


def open_keyer(how: str, gpio: int = 3) -> Keyer:
    """cm108[:N], rigctld[:host[:port]], or a serial port such as COM5 or /dev/ttyUSB0 with
    an optional :rts or :dtr (RTS when left out)."""
    kind, _, rest = how.partition(":")
    try:
        if kind.lower() == "cm108":
            return Cm108(int(rest) if rest else None, gpio)
        if kind.lower() == "rigctld":
            host, _, port = rest.partition(":")
            return Rigctld(host or "localhost", int(port or RIGCTLD_PORT))
    except ValueError as e:
        raise SetupError(f"can't read PTT setting {how!r}: {e}") from e
    line = rest.lower() or "rts"
    if line not in ("rts", "dtr"):
        raise SetupError(f"serial PTT line must be rts or dtr, not {rest!r}")
    return SerialLine(kind, line)


def choices() -> list[tuple[str, str]]:
    """(label, how) for every way to key that this machine offers right now."""
    out = []
    try:
        out += [(f"{describe(d)}, GPIO", f"cm108:{i}") for i, d in enumerate(find())]
    except SetupError:
        pass
    try:
        from serial.tools import list_ports

        for p in sorted(list_ports.comports(), key=lambda p: p.device):
            out.append((f"{p.device} RTS, {p.description}", p.device))
            out.append((f"{p.device} DTR, {p.description}", f"{p.device}:dtr"))
    except ImportError:
        pass
    out.append((f"CAT through Hamlib rigctld on localhost:{RIGCTLD_PORT}", "rigctld"))
    return out
