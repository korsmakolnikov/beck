"""kent's interface stubs in a project: those left, and those no id cites (README, "A clause already met").

kent writes every interface body as a stub (`STUBS`): the cycles of the
clauses the body's symbol cites replace it. A stub no id cites, through its
own docstring, an enclosing one or its file's, has no cycle to replace it.
"""

from __future__ import annotations

from pathlib import Path

from beck.index.anchor import ids_at, is_test_file
from beck.toolchain import STUBS, load


def stubs(root: Path) -> list[tuple[str, int]]:
    """Every stub body left in the project's sources, as (file, line)."""
    toolchain = load(root)
    marker, suffix = STUBS[toolchain.language]
    found = []
    for base in toolchain.source_roots(root):
        for path in sorted((root / base).rglob(f"*{suffix}")):
            relative = path.relative_to(root)
            if ".beck" in relative.parts or is_test_file(relative):
                continue
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if marker in line:
                    found.append((str(relative), n))
    return found


def uncited(root: Path) -> list[tuple[str, int]]:
    """The stubs no id cites: no cycle would ever implement them."""
    by_file: dict[str, set[int]] = {}
    for file, line in stubs(root):
        by_file.setdefault(file, set()).add(line)
    out = []
    for file, lines in by_file.items():
        cited = ids_at(root, Path(file), lines)
        out += [(file, n) for n in sorted(lines) if not cited.get(n)]
    return out
