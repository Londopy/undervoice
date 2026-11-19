"""Payload format: callsign, grid square and status packed into 32 bytes plus a CRC."""

from __future__ import annotations

import binascii
import re
from dataclasses import dataclass

ALPHABET = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/-."
CALL_LEN = 10
STATUS_LEN = 32
CALL_BITS = 54
GRID_BITS = 25
STATUS_BITS = 171
VERSION = 0

GRID6_COUNT = 18 * 18 * 10 * 10 * 24 * 24
GRID4_COUNT = 18 * 18 * 10 * 10
NO_GRID = (1 << GRID_BITS) - 1
GRID_RE = re.compile(r"[A-R]{2}[0-9]{2}([A-X]{2})?")


@dataclass(frozen=True)
class Message:
    """A callsign, an optional 4 or 6 character grid square and a short status."""

    call: str
    grid: str = ""
    status: str = ""

    def __post_init__(self) -> None:
        call = self.call.strip().upper()
        status = self.status.strip().upper()
        grid = self.grid.strip().upper()
        if not call:
            raise ValueError("callsign is empty")
        _check_text("callsign", call, CALL_LEN)
        _check_text("status", status, STATUS_LEN)
        if grid and not GRID_RE.fullmatch(grid):
            raise ValueError(f"grid {self.grid!r} is not a 4 or 6 character Maidenhead locator")
        object.__setattr__(self, "call", call)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "grid", grid[:4] + grid[4:].lower())

    def __str__(self) -> str:
        return " ".join(part for part in (self.call, self.grid, self.status) if part)


def _check_text(name: str, text: str, length: int) -> None:
    if len(text) > length:
        raise ValueError(f"{name} {text!r} is longer than {length} characters")
    bad = sorted({ch for ch in text if ch not in ALPHABET})
    if bad:
        raise ValueError(f"{name} has characters outside A-Z 0-9 / - . and space: {''.join(bad)!r}")


def _text_value(text: str, length: int) -> int:
    value = 0
    for ch in text.ljust(length):
        value = value * len(ALPHABET) + ALPHABET.index(ch)
    return value


def _text(value: int, length: int) -> str | None:
    if value >= len(ALPHABET) ** length:
        return None
    chars = []
    for _ in range(length):
        value, i = divmod(value, len(ALPHABET))
        chars.append(ALPHABET[i])
    return "".join(reversed(chars)).rstrip()


def _grid_value(grid: str) -> int:
    if not grid:
        return NO_GRID
    g = grid.upper()
    value = ((ord(g[0]) - 65) * 18 + ord(g[1]) - 65) * 100 + int(g[2:4])
    if len(g) == 4:
        return GRID6_COUNT + value
    return (value * 24 + ord(g[4]) - 65) * 24 + ord(g[5]) - 65


def _grid(value: int) -> str | None:
    if value == NO_GRID:
        return ""
    if value >= GRID6_COUNT:
        value -= GRID6_COUNT
        if value >= GRID4_COUNT:
            return None
        sub = ""
    else:
        value, b = divmod(value, 24)
        value, a = divmod(value, 24)
        sub = chr(97 + a) + chr(97 + b)
    value, square = divmod(value, 100)
    f1, f2 = divmod(value, 18)
    return f"{chr(65 + f1)}{chr(65 + f2)}{square:02d}{sub}"


def pack(message: Message) -> bytes:
    """Return the 34 byte frame payload: 256 data bits then a CRC-16."""
    value = VERSION
    value = (value << CALL_BITS) | _text_value(message.call, CALL_LEN)
    value = (value << GRID_BITS) | _grid_value(message.grid)
    value = (value << STATUS_BITS) | _text_value(message.status, STATUS_LEN)
    data = (value << 4).to_bytes(32, "big")
    return data + binascii.crc_hqx(data, 0xFFFF).to_bytes(2, "big")


def unpack(payload: bytes) -> Message | None:
    """Parse a payload, or return None if the CRC or any field is invalid."""
    data, crc = payload[:32], payload[32:]
    if binascii.crc_hqx(data, 0xFFFF).to_bytes(2, "big") != crc:
        return None
    value = int.from_bytes(data, "big")
    if value & 0xF or value >> 254 != VERSION:
        return None
    status = _text((value >> 4) & ((1 << STATUS_BITS) - 1), STATUS_LEN)
    grid = _grid((value >> (4 + STATUS_BITS)) & NO_GRID)
    call = _text((value >> (4 + STATUS_BITS + GRID_BITS)) & ((1 << CALL_BITS) - 1), CALL_LEN)
    if call is None or grid is None or status is None or not call:
        return None
    return Message(call, grid, status)
