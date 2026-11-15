"""beck's command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from beck import checks as static_checks
from beck.dsl.validate import validate_architecture, validate_domain
from beck.events.project import Project
from beck.events.store import Event
from beck.fitness.generate import write as write_fitness
from beck.index import plan
from beck.index.build import build
from beck.index.store import connect, data
from beck.testrun import TestRunError
from beck.testrun import run as run_tests
from beck.toolchain import ToolchainError
from beck.toolchain import load as load_toolchain


def _load(path: Path) -> object:
    try:
        return yaml.safe_load(path.read_text())
    except FileNotFoundError:
        sys.exit(f"{path}: not found")
    except yaml.YAMLError as error:
        sys.exit(f"{path}: not valid YAML: {error}")


def _validate(args: argparse.Namespace) -> int:
    beck_dir = Path(args.project) / ".beck"
    domain = _load(beck_dir / "domain.yaml")
    if args.dsl == "domain":
        problems = validate_domain(domain)
    else:
        problems = validate_architecture(_load(beck_dir / "architecture.yaml"), domain)
    for problem in problems:
        print(problem)
    if not problems:
        print("ok")
    return 1 if problems else 0


def _index_build(args: argparse.Namespace) -> int:
    root = Path(args.project)
    beck_dir = root / ".beck"
    conn, report = build(root, _load(beck_dir / "domain.yaml"), _load(beck_dir / "architecture.yaml"),
                         beck_dir / "index.db")
    for problem in report.problems:
        print(problem)
    if conn is None:
        return 1
    print(f"indexed {report.files} files, {report.markers} markers")
    for op in report.unanchored:
        print(f"unanchored: {op}")
    for m in report.unknown:
        print(f"unknown id: {m.path}:{m.line}: spec: {m.id}")
    return 0


def _unit_index(root: Path):
    """The index a unit's command reads: in a unit's worktree, the worktree's code (its tests are there, not yet
    in the main tree) against the project's model ($BECK_PROJECT/.beck), built in memory; else the project's."""
    project = _toolchain_root(root)
    beck_dir = project / ".beck"
    if project.resolve() == root.resolve() or not (beck_dir / "domain.yaml").is_file():
        return _db(argparse.Namespace(project=str(project)))
    conn, report = build(root, _load(beck_dir / "domain.yaml"), _load(beck_dir / "architecture.yaml"), ":memory:")
    if conn is None:
        sys.exit("\n".join(str(p) for p in report.problems) or "the model builds no index")
    return conn


def _db(args: argparse.Namespace):
    path = Path(args.project) / ".beck" / "index.db"
    if not path.exists():
        sys.exit(f"{path}: not found, run beck index build")
    return connect(path)


def _index_plan(args: argparse.Namespace) -> int:
    conn = _db(args)
    for unit in plan.units(conn):
        print(unit)
        for g in plan.groups(conn, unit):
            print(f"  {g.id}")
            for clause in g.clauses:
                print(f"    {clause}")
    for file, components in plan.shared_files(conn).items():
        print(f"shared: {file}: {', '.join(components)}")
    return 0


def _index_context(args: argparse.Namespace) -> int:
    try:
        print(json.dumps(plan.context(_db(args), args.unit, args.group), indent=2))
    except KeyError as error:
        sys.exit(str(error.args[0]))
    return 0


def _fitness_generate(args: argparse.Namespace) -> int:
    root = Path(args.project)
    domain, arch = _load(root / ".beck" / "domain.yaml"), _load(root / ".beck" / "architecture.yaml")
    problems = validate_architecture(arch, domain)
    for problem in problems:
        print(problem)
    if problems:
        return 1
    try:
        written = write_fitness(root, arch, args.stack)  # type: ignore[arg-type]
    except ValueError as error:
        sys.exit(str(error))
    for path in written.files:
        print(f"wrote {path}")
    for id_, rule in written.pending_rules.items():
        print(f"untranslated: {id_}: {rule} (write it in {written.rules_file}, citing spec: {id_})")
    for note in written.notes:
        print(f"note: {note}")
    return 0


def _project(args: argparse.Namespace) -> Project:
    root = Path(args.project)
    if not (root / ".beck" / "beck.db").exists():
        sys.exit(f"{root / '.beck'}: no beck project here")
    return Project(root)


def _iteration(project: Project, prefix: str):
    try:
        return project.find(prefix)
    except KeyError as error:
        sys.exit(str(error.args[0]))


def _line(e: Event) -> str:
    cause = e.causation_id[:8] if e.causation_id else "-"
    return (f"{e.seq:>5} {e.at} {e.id[:8]} cause={cause} corr={e.correlation_id[:8]} "
            f"{e.stream:<20} {e.actor:<10} {e.type} {json.dumps(e.payload)}")


def _sessions(args: argparse.Namespace) -> int:
    project = _project(args)
    for it in project.iterations():
        store = project.open(it.id)
        last = store.last()
        store.close()
        print(f"{it.id[:8]}  {it.name or '-':<30} {it.started_at}  {it.ended_at or 'open':<29}  "
              f"{f'{last.type} {last.at}' if last else '-'}")
    return 0


def _events(args: argparse.Namespace) -> int:
    project = _project(args)
    store = project.open(_iteration(project, args.iteration).id)
    for e in store.read(stream=args.stream, correlation_id=_full_id(store, args.correlation)):
        print(_line(e))
    return 0


def _full_id(store, prefix: str | None) -> str | None:
    if prefix is None:
        return None
    ids = [e.id for e in store.read() if e.id.startswith(prefix)]
    if len(ids) != 1:
        sys.exit(f"{len(ids)} events match {prefix!r}")
    return ids[0]


def _trace(args: argparse.Namespace) -> int:
    project = _project(args)
    store = project.open(_iteration(project, args.iteration).id)
    id_ = _full_id(store, args.event)
    assert id_ is not None
    for e in store.causes(id_):
        print(("> " if e.id == id_ else "  ") + _line(e))
    for e in store.effects(id_):
        print("  " + _line(e))
    return 0


def _toolchain_root(root: Path) -> Path:
    """The toolchain and the model are the project's: $BECK_PROJECT when an agent runs in a unit's worktree."""
    return Path(os.environ["BECK_PROJECT"]) if os.environ.get("BECK_PROJECT") else root


def _test(args: argparse.Namespace) -> int:
    root = Path(args.project)
    try:
        toolchain = load_toolchain(_toolchain_root(root))
    except ToolchainError as error:
        sys.exit(str(error))
    tests = list(args.tests)
    if not tests and (args.clause or args.unit):
        tests = _indexed_tests(root, toolchain, args.unit, args.clause)
    try:
        store_root = Path(os.environ["BECK_PROJECT"]) if os.environ.get("BECK_PROJECT") else None
        result = run_tests(root, toolchain, tests, actor=os.environ.get("BECK_ACTOR", "user"),
                           unit=args.unit, clause=args.clause, store_root=store_root)
    except TestRunError as error:
        sys.exit(str(error))
    print(result.summary)
    print(f"(TestsRun {result.event.id})")
    return result.exit_code


def _check(args: argparse.Namespace) -> int:
    root = Path(args.project)
    try:
        toolchain = load_toolchain(_toolchain_root(root))
    except ToolchainError as error:
        sys.exit(str(error))
    paths = list(args.paths)
    if not paths and args.unit:
        conn = _unit_index(root)
        row = conn.execute("SELECT data FROM node WHERE id = ? AND kind = 'component'", (args.unit,)).fetchone()
        if row is None:
            sys.exit(f"no component {args.unit!r} in the index")
        paths = toolchain.component_paths(root, json.loads(row["data"])["namespace"])
        if not paths:
            sys.exit(f"component {args.unit!r} has no files yet")
    if not paths:
        if not args.all:
            sys.exit("name the paths, a --unit, or --all")
        paths = ["."]
    try:
        result = static_checks.run(root, toolchain, paths, whole_project=args.all,
                                   actor=os.environ.get("BECK_ACTOR", "user"), unit=args.unit,
                                   store_root=_toolchain_root(root))
    except TestRunError as error:
        sys.exit(str(error))
    print(result.summary)
    print(f"(ChecksRun {result.event.id})")
    return result.exit_code


def _indexed_tests(root: Path, toolchain, unit: str | None, clause: str | None) -> list[str]:
    """The tests the index knows for a clause, or for every clause a unit realizes."""
    conn = _unit_index(root)
    if clause:
        clauses = [clause]
    else:
        clauses = [c for g in plan.groups(conn, unit or "") for c in g.clauses]
    rows = conn.execute(
        f"SELECT DISTINCT n.* FROM edge e JOIN node n ON n.id = e.src WHERE e.kind = 'tests' "
        f"AND e.dst IN ({','.join('?' * len(clauses))}) ORDER BY n.id", clauses).fetchall()
    if not rows:
        sys.exit("the index knows no test for that: name the tests")
    return [toolchain.selector(data(r)["file"], r["id"].split("::", 1)[1], data(r)["line"]) for r in rows]


def _start(args: argparse.Namespace) -> int:
    from beck import app
    return app.run(Path(args.project), None, False if args.batch else None)


def _attach(args: argparse.Namespace) -> int:
    from beck import app
    try:
        return app.run(Path(args.project), args.iteration, False if args.batch else None)
    except KeyError as error:
        sys.exit(str(error.args[0]))


def _tui(args: argparse.Namespace) -> int:
    from beck import tui
    try:
        return tui.run(Path(args.project), args.iteration)
    except KeyError as error:
        sys.exit(str(error.args[0]))


def _pending(args: argparse.Namespace) -> int:
    from beck import app
    return app.pending(Path(args.project), args.iteration)


def _answer(args: argparse.Namespace) -> int:
    from beck import app
    return app.answer(Path(args.project), args.iteration, args.command_id, " ".join(args.words))


def _audit(args: argparse.Namespace) -> int:
    from beck import argyris
    project = _project(args)
    if args.message:
        doc = argyris.message(project)
        if doc is None:
            return 1
        print(json.dumps(doc, indent=1))
        return 0
    if args.wake:
        return argyris.wake(project)
    if args.settle:
        try:
            doc = json.loads(sys.stdin.read())
        except json.JSONDecodeError as error:
            print(f"beck: the answer is not JSON: {error}", file=sys.stderr)
            return 1
        errors = argyris.settle(project, doc)
        for e in errors:
            print(f"beck: {e}", file=sys.stderr)
        return 1 if errors else 0
    if args.handle:
        project.handle(args.handle, args.by)
        return 0
    for r in project.pending():
        print(f"{r.id[:8]} {r.created_at} {r.kind:<24} event={r.event_id[:8]} corr={r.correlation_id[:8]} "
              f"{json.dumps(r.payload)}")
    return 0


def _contrib(args: argparse.Namespace) -> int:
    from beck import config as beck_config
    from beck import contrib
    try:
        config = beck_config.load(Path(args.project))
        if args.contrib_command == "start":
            print(contrib.start(args.slug, config))
        elif args.contrib_command == "diff":
            print(contrib.diff(args.slug))
        else:
            print(contrib.submit(args.slug, args.title, sys.stdin.read(), config))
    except (contrib.ContribError, beck_config.ConfigError) as error:
        print(f"beck: {error}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="beck")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="validate a DSL of .beck/ (docs/dsl.md)")
    validate.add_argument("dsl", choices=["domain", "architecture"])
    validate.add_argument("--project", default=".", help="the project's root (default: .)")
    validate.set_defaults(run=_validate)

    index = sub.add_parser("index", help="the DSL-to-code index in .beck/index.db")
    index_sub = index.add_subparsers(dest="index_command", required=True)
    for name, run, help_ in [("build", _index_build, "rebuild the index from the DSLs and the code"),
                             ("plan", _index_plan, "print the units, their groups and cycles"),
                             ("context", _index_context, "print the context of one group, as JSON")]:
        cmd = index_sub.add_parser(name, help=help_)
        cmd.add_argument("--project", default=".", help="the project's root (default: .)")
        cmd.set_defaults(run=run)
        if name == "context":
            cmd.add_argument("unit")
            cmd.add_argument("group", help="entity:<Name>, operation:<id> or model")

    fitness = sub.add_parser("fitness", help="the fitness functions of .beck/architecture.yaml")
    fitness_sub = fitness.add_subparsers(dest="fitness_command", required=True)
    generate = fitness_sub.add_parser("generate", help="write the generated fitness functions")
    generate.add_argument("--project", default=".", help="the project's root (default: .)")
    generate.add_argument("--stack", choices=["python", "elixir"], help="default: from mix.exs or pyproject.toml")
    generate.set_defaults(run=_fitness_generate)

    sessions = sub.add_parser("sessions", help="the iterations of the project and their last event")
    sessions.add_argument("--project", default=".", help="the project's root (default: .)")
    sessions.set_defaults(run=_sessions)
    events = sub.add_parser("events", help="the events of an iteration")
    events.add_argument("iteration", help="an iteration's id, or its first characters")
    events.add_argument("--stream")
    events.add_argument("--correlation", help="only the chain of this event (id or prefix)")
    events.add_argument("--project", default=".", help="the project's root (default: .)")
    events.set_defaults(run=_events)
    trace = sub.add_parser("trace", help="what caused an event, and what it caused")
    trace.add_argument("iteration")
    trace.add_argument("event", help="an event's id, or its first characters")
    trace.add_argument("--project", default=".", help="the project's root (default: .)")
    trace.set_defaults(run=_trace)

    test = sub.add_parser("test", help="run tests and record the run (TestsRun) as evidence")
    test.add_argument("tests", nargs="*", help="the runner's selectors; default: the index's tests of --clause or --unit")
    test.add_argument("--clause")
    test.add_argument("--unit")
    test.add_argument("--project", default=".", help="the project's root (default: .)")
    test.set_defaults(run=_test)

    check = sub.add_parser("check", help="run the static checks and record the run (ChecksRun)")
    check.add_argument("paths", nargs="*", help="default: the files of --unit; with --all, the project")
    check.add_argument("--unit")
    check.add_argument("--all", action="store_true", help="also the checks of the whole project (dialyzer)")
    check.add_argument("--project", default=".", help="the project's root (default: .)")
    check.set_defaults(run=_check)

    for name, run, help_ in [("start", _start, "start an iteration"),
                             ("attach", _attach, "resume an iteration from its events")]:
        cmd = sub.add_parser(name, help=help_)
        if name == "attach":
            cmd.add_argument("iteration")
        cmd.add_argument("--batch", action="store_true",
                         help="stop at the first decision of the user instead of asking it")
        cmd.add_argument("--project", default=".", help="the project's root (default: .)")
        cmd.set_defaults(run=run)
    t = sub.add_parser("tui", help="start an iteration, or resume one, in the TUI")
    t.add_argument("iteration", nargs="?")
    t.add_argument("--project", default=".", help="the project's root (default: .)")
    t.set_defaults(run=_tui)
    p = sub.add_parser("pending", help="the decisions of the user an iteration waits for")
    p.add_argument("iteration")
    p.add_argument("--project", default=".", help="the project's root (default: .)")
    p.set_defaults(run=_pending)
    a = sub.add_parser("answer", help="answer a pending decision (yes, or the words)")
    a.add_argument("iteration")
    a.add_argument("command_id", help="the decision's id, or its first characters")
    a.add_argument("words", nargs="+")
    a.add_argument("--project", default=".", help="the project's root (default: .)")
    a.set_defaults(run=_answer)

    au = sub.add_parser("audit", help="smart_audit's outbox: the violations not handled yet")
    mode = au.add_mutually_exclusive_group()
    mode.add_argument("--message", action="store_true", help="print the pending records as argyris's message (JSON)")
    mode.add_argument("--wake", action="store_true", help="open argyris's session with the message")
    mode.add_argument("--settle", action="store_true", help="read argyris's JSON answer on stdin, mark the records")
    mode.add_argument("--handle", help="mark a record (its id) handled")
    au.add_argument("--by", default="argyris")
    au.add_argument("--project", default=".", help="the project's root (default: .)")
    au.set_defaults(run=_audit)

    co = sub.add_parser("contrib", help="argyris's changes to beck, as pull requests")
    co_sub = co.add_subparsers(dest="contrib_command", required=True)
    for name, help_ in [("start", "a worktree of beck's source for one change; prints its path"),
                        ("diff", "what the change's worktree changes"),
                        ("submit", "commit, push and open the pull request (the body on stdin); prints its URL")]:
        c = co_sub.add_parser(name, help=help_)
        c.add_argument("slug")
        if name == "submit":
            c.add_argument("--title", required=True, help="a Conventional Commit subject")
        c.add_argument("--project", default=".", help="the project whose config names beck's repository")
        c.set_defaults(run=_contrib)
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
