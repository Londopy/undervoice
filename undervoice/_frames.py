"""What a modulator sends, frame after frame."""

from __future__ import annotations

import queue
import threading
from collections import deque
from collections.abc import Callable, Iterable

import numpy as np

from ._message import Message, Part, pack


class Frames:
    """Queued message parts in order, then the station frame again and again. render turns a
    payload into whatever the modulator steps through. A part leaves the queue only once its
    whole frame has gone out, so one cut off when the radio unkeys goes again in the next over.
    Parts can be queued and cancelled from another thread; each one sent arrives on `sent`."""

    def __init__(self, station: Message, render: Callable[[bytes], np.ndarray]):
        self._render = render
        self._station = render(pack(station))
        self._queue: deque[tuple[Part, np.ndarray]] = deque()
        self._current: tuple[Part, np.ndarray] | None = None
        self._lock = threading.Lock()
        self.sent: queue.Queue[Part] = queue.Queue()

    def send(self, parts: Iterable[Part]) -> None:
        # rendered here, on the caller's thread, so the sound card thread never waits on it
        rendered = [(p, self._render(pack(p))) for p in parts]
        with self._lock:
            self._queue.extend(rendered)

    def cancel(self) -> list[Part]:
        """Drop every part not yet sent and return them. One already going out finishes its
        frame, but is returned with the rest and never arrives on `sent`."""
        with self._lock:
            parts = [p for p, _ in self._queue]
            self._queue.clear()
            return parts

    @property
    def waiting(self) -> list[Part]:
        with self._lock:
            return [p for p, _ in self._queue]

    def start(self) -> np.ndarray:
        """The next frame to send."""
        with self._lock:
            self._current = self._queue[0] if self._queue else None
            return self._station if self._current is None else self._current[1]

    def finish(self) -> None:
        """The frame from the last start() has gone out whole."""
        with self._lock:
            current, self._current = self._current, None
            if current is not None and self._queue and self._queue[0] is current:
                self._queue.popleft()
                self.sent.put(current[0])
