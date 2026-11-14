"""The human-readable flow log, .beck/beck.log: every line carries the event it comes from.

Code that handles an event runs inside `about(event)`; every log record
written there gets the event's id and correlation id, so a line of the log
leads back to the event store deterministically.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from beck.events.store import Event

_CURRENT: contextvars.ContextVar[Event | None] = contextvars.ContextVar("beck_event", default=None)

FORMAT = "%(asctime)s %(levelname)s [event=%(event_id)s correlation=%(correlation_id)s] %(name)s: %(message)s"


class EventContext(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        event = _CURRENT.get()
        record.event_id = event.id if event else "-"
        record.correlation_id = event.correlation_id if event else "-"
        return True


@contextmanager
def about(event: Event) -> Generator[None]:
    token = _CURRENT.set(event)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def setup(path: Path | str, level: int = logging.INFO) -> logging.Handler:
    """Sends beck's log to path, each line with its event; returns the handler."""
    handler = logging.FileHandler(path)
    handler.addFilter(EventContext())
    handler.setFormatter(logging.Formatter(FORMAT))
    logger = logging.getLogger("beck")
    logger.addHandler(handler)
    logger.setLevel(level)
    return handler
