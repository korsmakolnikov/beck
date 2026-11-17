"""Helpers for the fitness functions beck generates from .beck/architecture.yaml.

Written into the project by `beck fitness generate`; do not edit, it is
rewritten at every generation. Dependencies are read statically from the
``import`` statements with :mod:`ast`, so nothing is imported or executed.
Imports guarded by ``TYPE_CHECKING`` count too: a dependency for typing is
still a dependency. Dynamic imports (``importlib.import_module``) are not seen.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Module:
    name: str
    path: Path


def within(module: str, namespace: str) -> bool:
    return module == namespace or module.startswith(namespace + ".")


def modules(source_root: Path | str, package: str) -> list[Module]:
    """All the modules of ``package`` under ``source_root``."""
    root = Path(source_root)
    package_dir = root / package.replace(".", "/")
    assert package_dir.is_dir(), f"Package {package!r} not found under {root}."
    found = []
    for path in sorted(package_dir.rglob("*.py")):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        found.append(Module(".".join(parts), path))
    return found


def _resolve_relative(module: Module, level: int, target: str | None) -> str:
    package_parts = module.name.split(".")
    if module.path.name != "__init__.py":
        package_parts = package_parts[:-1]
    base = package_parts[: len(package_parts) - (level - 1)] if level > 1 else package_parts
    return ".".join([*base, target] if target else base)


def dependencies(module: Module, known: set[str] | None = None) -> set[str]:
    """The distinct absolute module names ``module`` imports. ``from pkg import mod``, when ``pkg.mod`` is one
    of the ``known`` modules, imports ``pkg.mod`` only, not the package around it."""
    tree = ast.parse(module.path.read_text(), filename=str(module.path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(module, node.level, node.module) if node.level else node.module or ""
            names = [f"{base}.{alias.name}" for alias in node.names if alias.name != "*"]
            submodules = [n for n in names if known is not None and n in known]
            if len(submodules) < len(node.names):   # a name of the package itself, not only its modules
                found.add(base)
            found.update(names)
    found.discard(module.name)
    return found


def assert_defined(source_root: Path | str, package: str, namespace: str) -> None:
    """Guards against vacuous passes: the namespace holds at least one module."""
    assert any(within(m.name, namespace) for m in modules(source_root, package)), (
        f"{namespace!r} has no modules in {package!r}."
    )


def defines_classes(module: Module) -> bool:
    """Whether the module defines a class: an entry point (``__main__``) or a re-export defines none."""
    tree = ast.parse(module.path.read_text(), filename=str(module.path))
    return any(isinstance(node, ast.ClassDef) for node in ast.walk(tree))


def assert_only_within(source_root: Path | str, package: str, namespaces: Iterable[str],
                       allow: Iterable[str] = ()) -> None:
    """contained_in: every module that defines a class lives within one of ``namespaces``, or is in ``allow``."""
    namespaces, allowed = list(namespaces), set(allow)
    strays = [m.name for m in modules(source_root, package)
              if m.name not in allowed and not any(within(m.name, ns) for ns in namespaces) and defines_classes(m)]
    assert not strays, f"Modules with classes outside {namespaces}: {strays}"


def assert_no_dependency(source_root: Path | str, package: str, origin: str, targets: Iterable[str]) -> None:
    """no_dependency: no module within ``origin`` imports a module within ``targets``."""
    targets = list(targets)
    found = modules(source_root, package)
    known = {m.name for m in found}
    violations = [(m.name, d) for m in found if within(m.name, origin)
                  for d in sorted(dependencies(m, known)) if any(within(d, t) for t in targets)]
    assert not violations, f"{origin} depends on {targets}: {violations}"


def assert_only_depends_on(source_root: Path | str, package: str, origin: str, targets: Iterable[str]) -> None:
    """only_depends_on: within the system, ``origin`` imports only itself and ``targets``.

    Modules outside ``package`` (the standard library, third parties) and the
    root module ``package`` itself are not judged.
    """
    allowed = [origin, *targets]
    found = modules(source_root, package)
    known = {m.name for m in found}
    violations = [(m.name, d) for m in found if within(m.name, origin)
                  for d in sorted(dependencies(m, known))
                  if within(d, package) and d != package and not any(within(d, a) for a in allowed)]
    assert not violations, f"{origin} depends outside {list(targets)}: {violations}"


def check(source_root: Path | str, package: str, form: str, origin: str | None,
          targets: list[str], allow: list[str]) -> None:
    """Runs one assertion of the generated table."""
    if form == "defined":
        assert_defined(source_root, package, targets[0])
    elif form == "contained_in":
        assert_only_within(source_root, package, targets, allow)
    elif form == "no_dependency":
        assert_no_dependency(source_root, package, origin or "", targets)
    elif form == "only_depends_on":
        assert_only_depends_on(source_root, package, origin or "", targets)
    else:
        raise AssertionError(f"unknown form {form!r}")
