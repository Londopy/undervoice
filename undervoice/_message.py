"""Payload formats, each packed into 32 bytes plus a CRC: a station frame (callsign, grid square
and status), or one part of a text message."""

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
# a text message part: from and to callsigns, which message, which part of how many, and text
TEXT_VERSION = 1
TEXT_ALPHABET = ALPHABET + "?,!':"
PART_LEN = 24
MAX_PARTS = 16
IDENT_BITS = 6
INDEX_BITS = 4
PART_BITS = 132

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


@dataclass(frozen=True)
class Part:
    """One frame of a text message from sender to `to` (blank for everyone). ident tells one
    message from the sender's others; index counts from 0 up to total - 1. Every part but the
    last carries exactly PART_LEN characters, so spaces where parts join survive."""

    sender: str
    to: str
    ident: int
    index: int
    total: int
    text: str

    def __post_init__(self) -> None:
        sender = self.sender.strip().upper()
        to = self.to.strip().upper()
        text = self.text.upper()
        if not sender:
            raise ValueError("callsign is empty")
        _check_text("callsign", sender, CALL_LEN)
        _check_text("to callsign", to, CALL_LEN)
        _check_text("message", text, PART_LEN, TEXT_ALPHABET)
        if not 0 <= self.ident < 1 << IDENT_BITS:
            raise ValueError(f"message number {self.ident} is outside 0 to {(1 << IDENT_BITS) - 1}")
        if not 0 <= self.index < self.total <= MAX_PARTS:
            raise ValueError(f"part {self.index + 1} of {self.total} is not a valid part")
        if self.index < self.total - 1 and len(text) != PART_LEN:
            raise ValueError(f"every part but the last carries {PART_LEN} characters")
        object.__setattr__(self, "sender", sender)
        object.__setattr__(self, "to", to)
        object.__setattr__(self, "text", text)

    def __str__(self) -> str:
        return f"{self.sender} > {self.to or 'ALL'} {self.index + 1}/{self.total}: {self.text}"


Payload = Message | Part


def normalize(text: str) -> str:
    """Message text as it goes on air: capitals, with every run of spaces or line breaks made
    one space."""
    return " ".join(text.upper().split())


def split(sender: str, to: str, text: str, ident: int) -> list[Part]:
    """The parts that carry text from sender to `to` as message number ident."""
    text = normalize(text)
    if not text:
        raise ValueError("the message is empty")
    if len(text) > PART_LEN * MAX_PARTS:
        raise ValueError(
            f"the message is {len(text)} characters; it can be {PART_LEN * MAX_PARTS} at most"
        )
    _check_text("message", text, len(text), TEXT_ALPHABET)
    chunks = [text[i : i + PART_LEN] for i in range(0, len(text), PART_LEN)]
    return [Part(sender, to, ident, i, len(chunks), c) for i, c in enumerate(chunks)]


def _check_text(name: str, text: str, length: int, alphabet: str = ALPHABET) -> None:
    if len(text) > length:
        raise ValueError(f"{name} {text!r} is longer than {length} characters")
    bad = sorted({ch for ch in text if ch not in alphabet})
    if bad:
        allowed = "A-Z 0-9 " + " ".join(alphabet[37:]) + " and space"
        raise ValueError(f"{name} has characters outside {allowed}: {''.join(bad)!r}")


def _text_value(text: str, length: int, alphabet: str = ALPHABET) -> int:
    value = 0
    for ch in text.ljust(length):
        value = value * len(alphabet) + alphabet.index(ch)
    return value


def _text(value: int, length: int, alphabet: str = ALPHABET, strip: bool = True) -> str | None:
    if value >= len(alphabet) ** length:
        return None
    chars = []
    for _ in range(length):
        value, i = divmod(value, len(alphabet))
        chars.append(alphabet[i])
    text = "".join(reversed(chars))
    return text.rstrip() if strip else text


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


def pack(payload: Payload) -> bytes:
    """Return the 34 byte frame payload: 256 data bits then a CRC-16."""
    if isinstance(payload, Part):
        value = TEXT_VERSION
        value = (value << CALL_BITS) | _text_value(payload.sender, CALL_LEN)
        value = (value << CALL_BITS) | _text_value(payload.to, CALL_LEN)
        value = (value << IDENT_BITS) | payload.ident
        value = (value << INDEX_BITS) | payload.index
        value = (value << INDEX_BITS) | (payload.total - 1)
        value = (value << PART_BITS) | _text_value(payload.text, PART_LEN, TEXT_ALPHABET)
    else:
        value = VERSION
        value = (value << CALL_BITS) | _text_value(payload.call, CALL_LEN)
        value = (value << GRID_BITS) | _grid_value(payload.grid)
        value = (value << STATUS_BITS) | _text_value(payload.status, STATUS_LEN)
        value <<= 4
    data = value.to_bytes(32, "big")
    return data + binascii.crc_hqx(data, 0xFFFF).to_bytes(2, "big")


def unpack(payload: bytes) -> Payload | None:
    """Parse a payload, or return None if the CRC or any field is invalid."""
    data, crc = payload[:32], payload[32:]
    if binascii.crc_hqx(data, 0xFFFF).to_bytes(2, "big") != crc:
        return None
    value = int.from_bytes(data, "big")
    if value >> 254 == TEXT_VERSION:
        return _unpack_part(value)
    if value & 0xF or value >> 254 != VERSION:
        return None
    status = _text((value >> 4) & ((1 << STATUS_BITS) - 1), STATUS_LEN)
    grid = _grid((value >> (4 + STATUS_BITS)) & NO_GRID)
    call = _text((value >> (4 + STATUS_BITS + GRID_BITS)) & ((1 << CALL_BITS) - 1), CALL_LEN)
    if call is None or grid is None or status is None or not call:
        return None
    return Message(call, grid, status)


def _unpack_part(value: int) -> Part | None:
    def take(bits: int) -> int:
        nonlocal value
        field = value & ((1 << bits) - 1)
        value >>= bits
        return field

    text_value = take(PART_BITS)
    total = take(INDEX_BITS) + 1
    index = take(INDEX_BITS)
    ident = take(IDENT_BITS)
    to = _text(take(CALL_BITS), CALL_LEN)
    sender = _text(take(CALL_BITS), CALL_LEN)
    text = _text(text_value, PART_LEN, TEXT_ALPHABET, strip=index == total - 1)
    if sender is None or to is None or text is None or not sender or index >= total:
        return None
    return Part(sender, to, ident, index, total, text)
