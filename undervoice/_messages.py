"""Text messages heard in parts, put back together, and the optional log of them on disk."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ._message import Part

GAP = "[...]"
# a part heard this long after the last one with the same number starts a new message
STALE = 3600.0


@dataclass
class Received:
    """A message as far as it has been heard. parts maps part index to text."""

    sender: str
    to: str
    ident: int
    total: int
    first: datetime
    last: datetime
    parts: dict[int, str] = field(default_factory=dict)
    logged: bool = False

    @property
    def complete(self) -> bool:
        return len(self.parts) == self.total

    @property
    def missing(self) -> list[int]:
        return [i for i in range(self.total) if i not in self.parts]

    @property
    def text(self) -> str:
        """The text with GAP where a part has not been heard."""
        return "".join(self.parts.get(i, GAP) for i in range(self.total))

    def fits(self, part: Part, when: datetime) -> bool:
        return (
            (self.sender, self.to, self.ident, self.total)
            == (part.sender, part.to, part.ident, part.total)
            and self.parts.get(part.index, part.text) == part.text
            and (when - self.last).total_seconds() < STALE
        )


class Inbox:
    """Every message heard, oldest first."""

    def __init__(self) -> None:
        self.messages: list[Received] = []

    def add(self, part: Part, when: datetime | None = None) -> Received | None:
        """File a heard part. Returns its message if this changed it, None for a repeat."""
        when = when or datetime.now().astimezone()
        for m in reversed(self.messages):
            if (m.sender, m.ident) == (part.sender, part.ident):
                if m.fits(part, when):
                    m.last = when
                    if part.index in m.parts:
                        return None
                    m.parts[part.index] = part.text
                    return m
                break
        m = Received(
            part.sender, part.to, part.ident, part.total, when, when, {part.index: part.text}
        )
        self.messages.append(m)
        return m


def line(when: datetime, sender: str, to: str, text: str) -> str:
    """One message as a line of the log."""
    return f"{when:%Y-%m-%d %H:%M}  {sender} > {to or 'ALL'}  {text}"


def heard_line(m: Received) -> str:
    text = m.text
    if not m.complete:
        missing = ", ".join(str(i + 1) for i in m.missing)
        noun = "part" if len(m.missing) == 1 else "parts"
        text += f"  (missing {noun} {missing} of {m.total})"
    return line(m.first, m.sender, m.to, text)


def append(path: str | Path, text: str) -> None:
    """Add a line to the log, starting the file if there is none."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(text + "\n")
