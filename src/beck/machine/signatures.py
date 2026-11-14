"""Failure signatures and the repetition rule (README, "Blocked cycles").

Every rejected attempt of a loop between an agent and a deterministic check
ends with a signature: the normalised set of what went wrong, without line
numbers, addresses, times or ids. A block of length p repeated 3 times at the
tail of the sequence blocks the loop; so do 30 attempts. The normalisation is
the delicate part: it starts here and is tuned with the replays.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence

_NOISE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0x_"),                                  # addresses
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{40,64}\b"), "<hash>"),
    (re.compile(r"\d+(\.\d+)?\s*(ms|s|seconds)\b"), "<time>"),
    (re.compile(r":\d+(:\d+)?\b"), ":_"),                                    # file:line:col
    (re.compile(r"\bline \d+\b"), "line _"),
    (re.compile(r"\s+"), " "),
]


def normalise(text: str) -> str:
    for pattern, replacement in _NOISE:
        text = pattern.sub(replacement, text)
    return text.strip()


def signature(items: Iterable[str]) -> str:
    """One short hash for the normalised set of what went wrong."""
    canonical = "\n".join(sorted({normalise(i) for i in items}))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def repeated(sequence: Sequence[str], times: int = 3) -> int | None:
    """The length p of a block repeated `times` in a row at the tail, if any."""
    for p in range(1, len(sequence) // times + 1):
        tail = sequence[-p * times:]
        block = tail[:p]
        if all(tail[i * p:(i + 1) * p] == block for i in range(times)):
            return p
    return None


def blocked(sequence: Sequence[str], times: int = 3, attempts: int = 30) -> str | None:
    """Why the loop is blocked, or None."""
    p = repeated(sequence, times)
    if p is not None:
        return f"the same {'result' if p == 1 else f'pattern of {p} results'} {times} times in a row"
    if len(sequence) >= attempts:
        return f"{attempts} attempts"
    return None
