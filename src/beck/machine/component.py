"""The machines of the components, of their units, and the scheduler of the slots (README, phase 4).

    ExecutionDerived | ComponentParked | ComponentImplemented | RestartApplied → ScheduleComponents
      [free slot] → StartComponent (a worktree from the feature branch) | ResumeComponent (rebased)
    ComponentStarted | UnitClosed → OpenUnit → UnitOpened, CycleOpened (first requirement)   | FinishComponent →
    ComponentImplemented
    CycleClosed → OpenCycle → CycleOpened (next requirement)                               | CloseUnit → UnitClosed

    ChangeRequested | CycleBlocked → ParkComponents (the component, the target's, their dependents) + ProposeRestart (kent)
    RestartProposed → ApproveRestart (the user: approves, or rejects with instructions)
    RestartRejected → DiscardRestart + ProposeRestart (with the instructions)
    RestartApproved → ApplyRestart (commit on the feature branch) → RestartApplied → ScheduleComponents

kent makes the changes of a restart in the feature branch's tree,
uncommitted, and describes them: the user reviews the real diff; beck commits
it on approval and discards it on rejection. A parked component holds no slot; a
restarted one is resumed first, rebased on the new base, its open cycles
reopened at their phase.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from beck.events.store import Event, EventStore
from beck.machine.core import Command, Ctx, Issued, Machine, Result
from beck.machine.cycle import cycle, green_stuck, undo_red
from beck.machine.cycle import stream as cycle_stream

components = Machine()

# --- the state machine of a component (component:<id>), its implementation (beck.machine.refactor and
# beck.machine.closing add the rest) --------------------------------------------------------------------------
#
#   new ─ComponentStarted→ started ─UnitOpened, UnitClosed…→ started ─ComponentImplemented→ implemented
#   ComponentParked: from any phase but closed → parked; ComponentResumed: back where it was parked
#   new units after an implementation (a restart added requirements): implemented → started
P = "component:"
WORKING = ("started", "implemented", "regressed", "reopened")
components.moves(P, "ComponentStarted", at=("new", "parked"), to="started")
components.moves(P, "ComponentParked", to="parked")
components.moves(P, "ComponentResumed", at=("parked",), to=lambda s, _: s.before("parked", "new") or "started")
components.moves(P, "UnitOpened", "UnitClosed", at=WORKING, to="started")
components.moves(P, "ComponentImplemented", at=WORKING, to="implemented")
components.finals(P, "closed")
components.runs(P, "StartComponent", "new", "parked")
components.runs(P, "ResumeComponent", "parked")
for _command in ("OpenUnit", "OpenCycle", "CloseUnit", "FinishComponent"):
    components.runs(P, _command, *WORKING)

# the state machine of the restarts (the stream `restart`): proposed, approved or rejected, applied
R = "restart"
components.moves(R, "RestartProposed", to="proposed")   # two blocked components may each propose one
components.moves(R, "RestartRejected", at=("proposed",), to="rejected")
components.moves(R, "RestartApproved", at=("proposed",), to="approved")
components.moves(R, "ExecutionDerived", at=("approved",))
components.moves(R, "RestartApplied", at=("approved",), to="applied")
components.runs(R, "ApplyRestart", "approved")
components.runs(R, "DiscardRestart", "rejected")


def stream(component: str) -> str:
    return f"component:{component}"


def _git(root: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=check)
    return out.stdout.strip()


# --- the workspace: where each component lives, from the events ------------------------------------------


@dataclass
class Workspace:
    """The components' worktrees come from their ComponentStarted; scopes and unit contexts from the index."""
    store: EventStore
    project: Path
    scope_of: Callable[[str, Path], list[str]]                 # (component, worktree) → the paths it may change
    unit_context_of: Callable[[str, str], dict[str, Any]]     # (component, unit) → beck index context
    requirement_text_of: Callable[[str], str] = lambda requirement: requirement  # the requirement's sentence, from the domain
    rederive_of: Callable[[], dict[str, Any] | None] = lambda: None  # ExecutionDerived's payload from the model of now
    _cache: dict[str, Path] = field(default_factory=dict)

    def rederive(self) -> dict[str, Any] | None:
        return self.rederive_of()

    def worktree(self, component: str) -> Path:
        if component not in self._cache:
            started = list(self.store.read(stream=stream(component), types_=("ComponentStarted", "ComponentReopened")))
            if not started:
                raise KeyError(f"component {component!r} has no worktree yet")
            self._cache[component] = Path(started[-1].payload["worktree"])
        return self._cache[component]

    def scope(self, component: str) -> list[str]:
        return self.scope_of(component, self.worktree(component))

    def unit_context(self, component: str, unit: str) -> dict[str, Any]:
        return self.unit_context_of(component, unit)

    def requirement_text(self, requirement: str) -> str:
        return self.requirement_text_of(requirement)


# --- read models, as of the policy's event ---------------------------------------------------------


def execution(ctx: Ctx) -> dict[str, dict[str, Any]]:
    scheduled = ctx.events(types=("ExecutionDerived",))
    return {u["id"]: u for u in scheduled[-1].payload["components"]} if scheduled else {}


def state(ctx: Ctx, component: str) -> str:
    """waiting | started | parked | restarted | implemented | closed."""
    s = "waiting"
    for e in ctx.events(types=("ComponentStarted", "ComponentParked", "ComponentResumed", "ComponentImplemented",
                               "ComponentClosed", "RestartApplied", "ComponentReopened")):
        p = e.payload
        if e.type == "RestartApplied":
            if component in p["components"] and s == "parked":
                s = "restarted" if _ever_started(ctx, component) else "waiting"
        elif p.get("component") == component:
            s = {"ComponentStarted": "started", "ComponentParked": "parked", "ComponentResumed": "started",
                 "ComponentImplemented": "implemented", "ComponentClosed": "closed", "ComponentReopened": "started"}[e.type]
    return s


def _ever_started(ctx: Ctx, component: str) -> bool:
    return bool(ctx.events(stream=stream(component), types=("ComponentStarted",)))


RUNNING = ("started", "implemented")


def _closed_requirements(ctx: Ctx, component: str) -> set[str]:
    return {e.payload["requirement"] for e in ctx.events(types=("CycleClosed",)) if e.payload["component"] == component}


def _units_closed(ctx: Ctx, component: str) -> set[str]:
    return {e.payload["unit"] for e in ctx.events(stream=stream(component), types=("UnitClosed",))}


def _current_unit(ctx: Ctx, component: str) -> str | None:
    opened = ctx.events(stream=stream(component), types=("UnitOpened",))
    return opened[-1].payload["unit"] if opened else None


# --- the scheduler -------------------------------------------------------------------------------------


@components.policy("ExecutionDerived", "ComponentParked", "ComponentImplemented", "ComponentClosed", "RestartApplied")
def schedule(event: Event, ctx: Ctx) -> list[Command]:
    # the scheduler reads the present, not the state as at its event: one ParkComponents writes many ComponentParked,
    # and at the first of them the others are still waiting; the StartComponents it issued are after its event too
    ctx = Ctx(**{**ctx.__dict__, "upto": None})
    scheduled = execution(ctx)
    states = {u: state(ctx, u) for u in scheduled}
    running = {u for u, s in states.items() if s in RUNNING}
    free = ctx.components_in_parallel - len(running)
    commands: list[Command] = []
    # the restarted components first, then the waiting ones, in the execution's (landing) order
    others_closed = all(s == "closed" for u, s in states.items() if not scheduled[u].get("integration"))
    for wanted, command in (("restarted", "ResumeComponent"), ("waiting", "StartComponent")):
        for u, s in states.items():
            if free <= 0:
                return commands
            if s != wanted or set(scheduled[u]["exclusive_with"]) & running:
                continue
            if scheduled[u].get("integration") and not others_closed:
                continue  # the integration component starts once every other component has landed
            if any(states.get(d) != "closed" for d in scheduled[u].get("waits_for", [])):
                continue  # its tests use the real modules of its concrete dependencies: it waits for them to land
            commands.append(Command(command, "beck", {"component": u}, stream(u)))
            running.add(u)
            free -= 1
    return commands


@components.handler("StartComponent")
def start_component(command: Issued, ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    path = ctx.project / ".beck" / "worktrees" / component
    branch = f"beck/{ctx.iteration[:8]}/{component}"
    base = _git(ctx.project, "rev-parse", "HEAD")
    if not path.exists():  # tolerate a crash after the worktree, before the event
        _git(ctx.project, "worktree", "add", "-q", "-b", branch, str(path), base)
    return [Result("ComponentStarted", {"component": component, "worktree": str(path), "branch": branch, "base": base})]


# --- units and cycles -----------------------------------------------------------------------------


@components.policy("ComponentStarted", "UnitClosed", "ComponentResumed")
def next_unit(event: Event, ctx: Ctx) -> list[Command]:
    component = event.payload["component"]
    if event.type == "ComponentResumed" and (_open_cycles(ctx, component) or
                                        (_implemented_before(ctx, component) and not _unwritten(ctx, component))):
        return []  # the reopened cycles go on; an implemented component with nothing new went back to phase 5
    return _walk(ctx, component)


def _walk(ctx: Ctx, component: str) -> list[Command]:
    """The next unit to open: one never closed, or one a restart gave a requirement no cycle closed yet."""
    done, closed = _units_closed(ctx, component), _closed_requirements(ctx, component)
    for g in execution(ctx)[component]["units"]:
        if g["id"] not in done or any(c not in closed for c in g["requirements"]):
            return [Command("OpenUnit", "beck", {"component": component, "unit": g["id"]}, stream(component))]
    return [Command("FinishComponent", "beck", {"component": component}, stream(component))]


def _unwritten(ctx: Ctx, component: str) -> list[str]:
    """The scheduled requirements no cycle closed: after a restart, those it added to the component."""
    closed = _closed_requirements(ctx, component)
    return [c for g in execution(ctx)[component]["units"] for c in g["requirements"] if c not in closed]


def _next_requirement(ctx: Ctx, component: str, unit: str) -> str | None:
    closed = _closed_requirements(ctx, component)
    requirements = next(g["requirements"] for g in execution(ctx)[component]["units"] if g["id"] == unit)
    return next((c for c in requirements if c not in closed), None)


@components.handler("OpenUnit")
def open_unit(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    requirement = _next_requirement(ctx, a["component"], a["unit"])
    results = [Result("UnitOpened", {"component": a["component"], "unit": a["unit"]})]
    if requirement is None:
        return results + [Result("UnitClosed", {"component": a["component"], "unit": a["unit"]})]
    return results + [Result("CycleOpened", {"component": a["component"], "requirement": requirement, "unit": a["unit"],
                                             "first": True},
                             stream=cycle_stream(a["component"], requirement))]


@components.policy("CycleClosed")
def next_cycle(event: Event, ctx: Ctx) -> list[Command]:
    component = event.payload["component"]
    if _implemented_before(ctx, component):  # a cycle reopened after the component was implemented, or one a restart added
        return [] if _open_cycles(ctx, component) else _walk(ctx, component)
    unit = _current_unit(ctx, component)
    if unit is None:
        return []
    if _next_requirement(ctx, component, unit) is None:
        return [Command("CloseUnit", "beck", {"component": component, "unit": unit}, stream(component))]
    return [Command("OpenCycle", "beck", {"component": component, "unit": unit}, stream(component))]


@components.handler("OpenCycle")
def open_cycle(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    requirement = _next_requirement(ctx, a["component"], a["unit"])
    assert requirement is not None
    return [Result("CycleOpened", {"component": a["component"], "requirement": requirement, "unit": a["unit"], "first": False},
                   stream=cycle_stream(a["component"], requirement))]


@components.handler("CloseUnit")
def close_unit(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("UnitClosed", {"component": a["component"], "unit": a["unit"]})]


@components.handler("FinishComponent")
def finish_component(command: Issued, ctx: Ctx) -> list[Result]:
    return [Result("ComponentImplemented", {"component": command.payload["args"]["component"]})]


# --- parking and restarts -----------------------------------------------------------------------


def _owner(ctx: Ctx, target: str) -> str | None:
    """The component a change's target belongs to: a component's id, the component that implements the id, or whose scope holds
    the file."""
    scheduled = execution(ctx)
    if target in scheduled:
        return target
    owner = next((u for u, p in scheduled.items() if target in p["implements"]), None)
    if owner is None and "/" in target:
        owner = next((u for u in scheduled if any(target == s or target.startswith(s.rstrip("/") + "/")
                                                for s in _scope(ctx, u))), None)
    return owner


def _scope(ctx: Ctx, component: str) -> list[str]:
    try:
        return list(ctx.workspace.scope(component))
    except KeyError:   # a component not started yet has no scope in the cycle's workspace
        return []


def _affected(ctx: Ctx, requester: str, target: str | None, kind: str | None = None) -> list[str]:
    """The components a change stops: the requester, the target's owner and, unless only a test changes, its dependents."""
    scheduled = execution(ctx)
    owner = _owner(ctx, target) if target else None
    hit = {requester} | ({owner} if owner else set())
    if kind != "test":   # a test is no component's interface: the dependents go on
        hit |= {u for u, p in scheduled.items() if owner and owner in p["depends_on"]}
    return [u for u in scheduled if u in hit and state(ctx, u) not in ("closed", "parked")]


@components.policy("ChangeRequested")
def park_for_a_change(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    if green_stuck(ctx, event):
        return []  # the cycle goes back to red (cycle.rewind_red): no component waits for a restart
    details = {k: p[k] for k in ("agent", "component", "target", "problem", "proposal")}
    return _park_and_propose(ctx, event, _affected(ctx, p["component"], p["target"], p["kind"]), p["kind"], details,
                             f"{p['kind']} change requested by {p['agent']}")


@components.policy("CycleBlocked")
def park_for_a_block(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    return _park_and_propose(ctx, event, _affected(ctx, p["component"], None), "blocked_cycle", dict(p),
                             f"cycle {p['requirement']} blocked: {p['reason']}")


def _park_and_propose(ctx: Ctx, event: Event, affected: list[str], kind: str, details: dict[str, Any],
                      reason: str) -> list[Command]:
    chain = event.id
    return [Command("ParkComponents", "beck", {"components": affected, "reason": reason, "because_of": chain}, "restart"),
            _propose(chain, kind, details, None)]


def _propose(chain: str, kind: str, details: dict[str, Any], instructions: str | None) -> Command:
    context: dict[str, Any] = {"kind": kind, "details": details}
    if instructions:
        context["instructions"] = instructions
    return Command("ProposeRestart", "kent", {"session": f"restart/{chain[:8]}", "context": context,
                                                 "chain": chain, "kind": kind, "details": details}, "restart")


@components.handler("ParkComponents")
def park_components(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("ComponentParked", {"component": u, "reason": a["reason"], "because_of": a["because_of"]}, stream=stream(u))
            for u in a["components"]]


@components.translator("ProposeRestart")
def restart_proposed(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("RestartProposed", dict(output["result"]), actor="kent")]


def _chain_args(ctx: Ctx, event: Event) -> dict[str, Any]:
    """The args of the ProposeRestart an event of the restart descends from."""
    for e in reversed(ctx.store.causes(event.id)):
        if e.type == "AgentTurnRequested" and e.payload["command"] == "ProposeRestart":
            return e.payload["args"]
    raise KeyError(f"{event.id} is not in a restart")


@components.policy("RestartProposed")
def ask_approval(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("ApproveRestart", "user", {"proposal": event.payload, "chain": _chain_args(ctx, event)["chain"]},
                    "restart")]


@components.policy("RestartRejected")
def propose_again(event: Event, ctx: Ctx) -> list[Command]:
    a = _chain_args(ctx, event)
    return [Command("DiscardRestart", "beck", {}, "restart"),
            _propose(a["chain"], a["kind"], a["details"], event.payload["instructions"])]


@components.handler("DiscardRestart")
def discard_restart(command: Issued, ctx: Ctx) -> list[Result]:
    _git(ctx.project, "reset", "-q", "--", ".", ":!.beck")   # a git rm is staged: unstage it, then restore
    _git(ctx.project, "checkout", "--", ".", ":!.beck")
    _git(ctx.project, "clean", "-fdq", "--", ".", ":!.beck")
    return []


@components.policy("RestartApproved")
def apply_restart(event: Event, ctx: Ctx) -> list[Command]:
    ask = ctx.store.get(event.causation_id or "")
    proposal = ask.payload["args"]["proposal"]
    return [Command("ApplyRestart", "beck", {"chain": ask.payload["args"]["chain"], "summary": proposal["summary"],
                                                "subject": proposal.get("subject") or proposal["summary"]},
                    "restart")]


@components.handler("ApplyRestart")
def handle_apply(command: Issued, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    _regenerate_fitness(ctx.project)
    _git(ctx.project, "add", "-A", "--", ".", ":!.beck")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ctx.project).returncode != 0:
        # the subject alone, one sentence; the summary is the body
        _git(ctx.project, "commit", "-q", "-m", f"refactor: {a.get('subject') or a['summary']}", "-m", a["summary"])
    parked = [e.payload["component"] for e in ctx.store.read(types_=("ComponentParked",))
              if e.payload["because_of"] == a["chain"]]
    results = []
    rederive = getattr(ctx.workspace, "rederive", None)
    scheduled = rederive() if rederive else None   # the restart may have changed the DSLs: the components' requirements with them
    if scheduled is not None and scheduled["components"] != list(execution(ctx).values()):
        results.append(Result("ExecutionDerived", scheduled))
    return [*results, Result("RestartApplied", {"base": _git(ctx.project, "rev-parse", "HEAD"), "components": parked,
                                                   "summary": a["summary"]})]


def _regenerate_fitness(project: Path) -> None:
    """The generated fitness functions follow the architecture a restart changed (the ping/pong pilot: a new
    depends_on stayed out of the generated table, and the component stayed stuck on it). The free-text rules
    kent translated are kept."""
    path = project / ".beck" / "architecture.yaml"
    if not path.is_file():
        return
    from beck.fitness.generate import write
    arch = yaml.safe_load(path.read_text())
    if isinstance(arch, dict):
        write(project, arch)


@components.handler("ResumeComponent")
def resume_component(command: Issued, ctx: Ctx) -> list[Result]:
    component = command.payload["args"]["component"]
    tree = ctx.workspace.worktree(component)
    base = _git(ctx.project, "rev-parse", "HEAD")
    _git(tree, "reset", "-q", "--hard")
    _git(tree, "clean", "-fdq", "--", ".", ":!.beck")
    rebased = subprocess.run(["git", "rebase", "-q", base], cwd=tree, capture_output=True, text=True)
    if rebased.returncode != 0:
        _git(tree, "rebase", "--abort", check=False)
        return [Result("ComponentParked", {"component": component, "reason": "the rebase on the restart conflicted",
                                      "because_of": command.id})]
    applied = [e for e in ctx.store.read(types_=("RestartApplied",)) if component in e.payload["components"]]
    reason = applied[-1].payload["summary"] if applied else "the interfaces changed"
    scheduled = {c for g in execution(ctx)[component]["units"] for c in g["requirements"]}
    dropped = [(c, g) for c, g in _open_cycles(ctx, component) if c not in scheduled]
    for c, _ in dropped:   # its red test would fail the component's tests as a regression of nothing
        undo_red(tree, component, c)
    # before ComponentResumed: the policies that react to it see the dropped cycles closed
    results = [Result("CycleDropped", {"component": component, "requirement": c, "reason": f"no longer in the domain: {reason}"},
                      stream=cycle_stream(component, c)) for c, _ in dropped]
    results.append(Result("ComponentResumed", {"component": component, "base": base}))
    if not [o for o in _open_cycles(ctx, component) if o not in dropped] and _implemented_before(ctx, component) \
            and not _unwritten(ctx, component):   # a requirement the restart added still needs its cycle: next_unit opens it
        results.append(Result("ComponentImplemented", {"component": component}))
    for requirement, unit in _open_cycles(ctx, component):
        if (requirement, unit) in dropped:
            continue
        phase = "green" if _red_committed(ctx, component, requirement) else "red"
        results.append(Result("CycleReopened", {"component": component, "requirement": requirement, "unit": unit, "phase": phase,
                                                "reason": f"resumed after a restart: {reason}"},
                              stream=cycle_stream(component, requirement)))
    return results


def _open_cycles(ctx: Ctx, component: str) -> list[tuple[str, str]]:
    opened: dict[str, str] = {}
    for e in ctx.events(types=("CycleOpened", "CycleReopened", "CycleClosed", "CycleDropped")):
        if e.payload["component"] != component:
            continue
        if e.type in ("CycleOpened", "CycleReopened"):
            opened[e.payload["requirement"]] = e.payload["unit"]
        else:
            opened.pop(e.payload["requirement"], None)
    return list(opened.items())


def _implemented_before(ctx: Ctx, component: str) -> bool:
    return bool(ctx.events(stream=stream(component), types=("ComponentImplemented",)))


def _red_committed(ctx: Ctx, component: str, requirement: str) -> bool:
    last = list(ctx.store.read(stream=cycle_stream(component, requirement), types_=("RedCommitted", "RedRewound")))
    return bool(last) and last[-1].type == "RedCommitted"


implementation = cycle.merge(components)
