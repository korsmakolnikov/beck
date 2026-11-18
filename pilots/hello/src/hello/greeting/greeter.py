"""Greets the world from the command line.

It is the whole program: the greeting and the entry point that writes it.
"""

import sys
from dataclasses import dataclass
from typing import TextIO


@dataclass(frozen=True)
class Greeting:
    """The fixed message the program greets the world with.

    It is a value object, so the message is named once and compared by value.

    Example: ``Greeting(text="Hello, world!").text``
    """

    text: str


def the_greeting() -> Greeting:
    """Return the greeting of the world.

    spec: greeting-text-is-hello-world (the greeting's text is fixed).

    >>> the_greeting().text
    'Hello, world!'
    """
    return Greeting(text="Hello, world!")


def main(out: TextIO | None = None) -> int:
    """Write the greeting and a newline on ``out``; return the exit status.

    spec: greet-the-world (the program run with no arguments greets the world).
    ``out`` is injected so a test captures the output without a subprocess.

    Example: ``status = main(io.StringIO())`` leaves ``"Hello, world!\\n"`` and 0.
    """
    # Resolved at call time so a redirected sys.stdout is honoured.
    (sys.stdout if out is None else out).write(the_greeting().text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
