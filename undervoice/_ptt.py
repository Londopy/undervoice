"""Keying a radio through the GPIO pin of a CM108-family USB sound card interface."""

from __future__ import annotations

from types import ModuleType, TracebackType
from typing import Any

# C-Media and SSS make the sound chips in most USB radio interfaces (DMK URI, RIM-Lite,
# Signalink-style clones); 1209:7388 is the AIOC cable, which copies the CM108 protocol
VENDORS = {0x0D8C, 0x0C76}
AIOC = (0x1209, 0x7388)


class SetupError(Exception):
    """A sound card or PTT interface can't be used as asked; the message says why."""


def _hid() -> ModuleType:
    try:
        import hid
    except ImportError as e:
        raise SetupError("PTT keying needs hidapi: pip install undervoice[live]") from e
    return hid


def find() -> list[dict[str, Any]]:
    """HID entries for every attached interface that can key PTT."""
    return [
        d
        for d in _hid().enumerate()
        if d["vendor_id"] in VENDORS or (d["vendor_id"], d["product_id"]) == AIOC
    ]


def describe(d: dict[str, Any]) -> str:
    name = d["product_string"] or d["manufacturer_string"] or "USB audio interface"
    return f"{name} ({d['vendor_id']:04x}:{d['product_id']:04x})"


class Ptt:
    """Holds an interface's PTT pin; unkeys when closed, so Ctrl+C never leaves the radio
    transmitting."""

    def __init__(self, choice: int | None = None, gpio: int = 3):
        found = find()
        if not found:
            raise SetupError("no CM108-type USB radio interface found for PTT")
        if choice is None and len(found) > 1:
            names = "; ".join(f"{i}: {describe(d)}" for i, d in enumerate(found))
            raise SetupError(f"more than one interface can key PTT, pick one with --ptt N: {names}")
        index = choice or 0
        if index >= len(found):
            raise SetupError(f"no PTT interface number {index}; undervoice devices lists them")
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

    def __enter__(self) -> Ptt:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()
