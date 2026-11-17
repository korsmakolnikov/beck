"""The agents' tools: the commands an agent sends the session from its turn (`beck send`, the socket).

The tests and the checks run here, once, inside beck: the run is recorded (TestsRun, ChecksRun), caused by
the agent's turn, and its summary goes back to the agent, which goes on with it. The verifiers read these
runs as the evidence of a phase.
"""

from __future__ import annotations

from pathlib import Path

from beck import checks as static_checks
from beck.machine.core import Ctx, Issued, Machine, Reply
from beck.testrun import run as run_tests
from beck.toolchain import load

tools = Machine()


def _folder(ctx: Ctx, args: dict[str, object]) -> Path:
    """Where the agent works: its cwd, inside the project (the project's tree or a component's worktree)."""
    folder = Path(str(args.get("cwd") or ctx.project)).resolve()
    project = ctx.project.resolve()
    if folder != project and project not in folder.parents:
        raise ValueError(f"{folder} is outside the project {project}")
    return folder


@tools.handler("RunTests")
def handle_run_tests(command: Issued, ctx: Ctx) -> Reply:
    a = command.payload["args"]
    run = run_tests(_folder(ctx, a), load(ctx.project), list(a.get("tests", [])), actor=str(a["actor"]),
                    component=a.get("component"), requirement=a.get("requirement"), store_root=ctx.project,
                    store=ctx.store, cause=command.cause)
    return Reply([], {"summary": run.summary, "exit_code": run.exit_code, "event": run.recorded.id})


@tools.handler("RunChecks")
def handle_run_checks(command: Issued, ctx: Ctx) -> Reply:
    a = command.payload["args"]
    run = static_checks.run(_folder(ctx, a), load(ctx.project), list(a.get("paths", [])),
                            whole_project=bool(a.get("all")), actor=str(a["actor"]), component=a.get("component"),
                            store_root=ctx.project, store=ctx.store, cause=command.cause)
    return Reply([], {"summary": run.summary, "exit_code": run.exit_code, "event": run.recorded.id})
