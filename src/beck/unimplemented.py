"""kent's interface unimplemented in a project: those left, and those no id references (README, "A requirement already met").

kent writes every interface body as an unimplemented (`UNIMPLEMENTED`): the cycles of the
requirements the body's symbol references replace it. An unimplemented no id references, through its
own docstring, an enclosing one or its file's, has no cycle to replace it.
"""

from __future__ import annotations

from pathlib import Path

from beck.index.anchor import ids_at, is_test_file
from beck.toolchain import UNIMPLEMENTED, Toolchain, load


def unimplemented(root: Path, toolchain: Toolchain | None = None) -> list[tuple[str, int]]:
    """Every unimplemented body left in the sources under root (the project, or a component's worktree with the project's
    toolchain), as (file, line)."""
    toolchain = toolchain or load(root)
    marker, suffix = UNIMPLEMENTED[toolchain.language]
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


def unreferenced(root: Path) -> list[tuple[str, int]]:
    """The unimplemented no id references: no cycle would ever implement them."""
    by_file: dict[str, set[int]] = {}
    for file, line in unimplemented(root):
        by_file.setdefault(file, set()).add(line)
    out = []
    for file, lines in by_file.items():
        referenced = ids_at(root, Path(file), lines)
        out += [(file, n) for n in sorted(lines) if not referenced.get(n)]
    return out
