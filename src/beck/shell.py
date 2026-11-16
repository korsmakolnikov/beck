"""A shell line split into its simple commands, as the CLI checks them."""

from __future__ import annotations

import re

_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


def _split(line: str) -> list[str]:
    """A line split on && || ; | outside quotes: `grep "a\\|b" f` is one command."""
    out, current, quote, i = [], "", None, 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(line):
                current += line[i:i + 2]
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch == "\\" and i + 1 < len(line):
            current += line[i:i + 2]
            i += 2
            continue
        elif ch in "'\"":
            quote = ch
        elif line.startswith(("&&", "||"), i) or ch in ";|":
            out.append(current)
            current = ""
            i += 2 if line.startswith(("&&", "||"), i) else 1
            continue
        current += ch
        i += 1
    out.append(current)
    return [p.strip() for p in out if p.strip()]


def parts(command: str) -> list[str]:
    """The simple commands of a shell line, as the CLI checks them: split on && || ; | (outside quotes) and
    newlines, a heredoc's body left out (it is data, not commands)."""
    out: list[str] = []
    ending: str | None = None
    for line in command.splitlines():
        if ending is not None:
            if line.strip() == ending:
                ending = None
            continue
        heredoc = _HEREDOC.search(line)
        if heredoc:
            ending = heredoc.group(1)
        out += _split(line)
    return out
