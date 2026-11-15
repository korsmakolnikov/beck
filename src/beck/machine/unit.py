"""The machines of the units, of their groups, and the scheduler of the slots (README, phase 4).

    UnitsPlanned | UnitParked | UnitImplemented | ResolutionApplied → ScheduleUnits
      [free slot] → StartUnit (a worktree from the feature branch) | ResumeUnit (rebased)
    UnitStarted | GroupClosed → OpenGroup → GroupOpened, CycleOpened (first clause)   | FinishUnit → UnitImplemented
    CycleClosed → OpenCycle → CycleOpened (next clause)                               | CloseGroup → GroupClosed

    ChangeRequested | CycleBlocked → ParkUnits (the unit, the target's, their dependents) + ProposeResolution (kent)
    ResolutionProposed → ApproveResolution (the user: approves, or rejects with instructions)
    ResolutionRejected → DiscardResolution + ProposeResolution (with the instructions)
    ResolutionApproved → ApplyResolution (commit on the feature branch) → ResolutionApplied → ScheduleUnits

kent makes the changes of a resolution in the feature branch's tree,
uncommitted, and describes them: the user reviews the real diff; beck commits
it on approval and discards it on rejection. A parked unit holds no slot; a
resolved one is resumed first, rebased on the new base, its open cycles
reopened at their phase.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beck.events.store import Event, EventStore
from beck.machine.core import Command, Ctx, Machine, Result
from beck.machine.cycle import cycle, green_stuck
from beck.machine.cycle import stream as cycle_stream

units = Machine()


def stream(unit: str) -> str:
    return f"unit:{unit}"


def _git(root: Path, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=check)
    return out.stdout.strip()


# --- the workspace: where each unit lives, from the events ------------------------------------------


@dataclass
class Workspace:
    """The units' worktrees come from their UnitStarted; scopes and group contexts from the index."""
    store: EventStore
    project: Path
    scope_of: Callable[[str, Path], list[str]]                 # (unit, worktree) → the paths it may change
    group_context_of: Callable[[str, str], dict[str, Any]]     # (unit, group) → beck index context
    clause_text_of: Callable[[str], str] = lambda clause: clause  # the clause's sentence, from the domain
    replan_of: Callable[[], dict[str, Any] | None] = lambda: None  # UnitsPlanned's payload from the model of now
    _cache: dict[str, Path] = field(default_factory=dict)

    def replan(self) -> dict[str, Any] | None:
        return self.replan_of()

    def worktree(self, unit: str) -> Path:
        if unit not in self._cache:
            started = list(self.store.read(stream=stream(unit), types_=("UnitStarted", "UnitReopened")))
            if not started:
                raise KeyError(f"unit {unit!r} has no worktree yet")
            self._cache[unit] = Path(started[-1].payload["worktree"])
        return self._cache[unit]

    def scope(self, unit: str) -> list[str]:
        return self.scope_of(unit, self.worktree(unit))

    def group_context(self, unit: str, group: str) -> dict[str, Any]:
        return self.group_context_of(unit, group)

    def clause_text(self, clause: str) -> str:
        return self.clause_text_of(clause)


# --- read models, as of the policy's event ---------------------------------------------------------


def plan(ctx: Ctx) -> dict[str, dict[str, Any]]:
    planned = ctx.events(types=("UnitsPlanned",))
    return {u["id"]: u for u in planned[-1].payload["units"]} if planned else {}


def state(ctx: Ctx, unit: str) -> str:
    """waiting | starting | started | parked | resolved | implemented | closed."""
    s = "waiting"
    for e in ctx.events(types=("CommandIssued", "UnitStarted", "UnitParked", "UnitResumed", "UnitImplemented",
                               "UnitClosed", "ResolutionApplied", "UnitReopened")):
        p = e.payload
        if e.type == "CommandIssued":
            if p["command"] in ("StartUnit", "ResumeUnit") and p["args"].get("unit") == unit:
                s = "starting"
        elif e.type == "ResolutionApplied":
            if unit in p["units"] and s == "parked":
                s = "resolved" if _ever_started(ctx, unit) else "waiting"
        elif p.get("unit") == unit:
            s = {"UnitStarted": "started", "UnitParked": "parked", "UnitResumed": "started",
                 "UnitImplemented": "implemented", "UnitClosed": "closed", "UnitReopened": "started"}[e.type]
    return s


def _ever_started(ctx: Ctx, unit: str) -> bool:
    return bool(ctx.events(stream=stream(unit), types=("UnitStarted",)))


RUNNING = ("starting", "started", "implemented")


def _closed_clauses(ctx: Ctx, unit: str) -> set[str]:
    return {e.payload["clause"] for e in ctx.events(types=("CycleClosed",)) if e.payload["unit"] == unit}


def _groups_closed(ctx: Ctx, unit: str) -> set[str]:
    return {e.payload["group"] for e in ctx.events(stream=stream(unit), types=("GroupClosed",))}


def _current_group(ctx: Ctx, unit: str) -> str | None:
    opened = ctx.events(stream=stream(unit), types=("GroupOpened",))
    return opened[-1].payload["group"] if opened else None


# --- the scheduler -------------------------------------------------------------------------------------


@units.policy("UnitsPlanned", "UnitParked", "UnitImplemented", "UnitClosed", "ResolutionApplied")
def schedule(event: Event, ctx: Ctx) -> list[Command]:
    # the scheduler reads the present, not the state as at its event: one ParkUnits writes many UnitParked,
    # and at the first of them the others are still waiting; the StartUnits it issued are after its event too
    ctx = Ctx(**{**ctx.__dict__, "upto": None})
    planned = plan(ctx)
    states = {u: state(ctx, u) for u in planned}
    running = {u for u, s in states.items() if s in RUNNING}
    free = ctx.units_in_parallel - len(running)
    commands: list[Command] = []
    # the resolved units first, then the waiting ones, in the plan's (landing) order
    others_closed = all(s == "closed" for u, s in states.items() if not planned[u].get("integration"))
    for wanted, command in (("resolved", "ResumeUnit"), ("waiting", "StartUnit")):
        for u, s in states.items():
            if free <= 0:
                return commands
            if s != wanted or set(planned[u]["exclusive_with"]) & running:
                continue
            if planned[u].get("integration") and not others_closed:
                continue  # the integration unit starts once every other unit has landed
            if any(states.get(d) != "closed" for d in planned[u].get("waits_for", [])):
                continue  # its tests use the real modules of its concrete dependencies: it waits for them to land
            commands.append(Command(command, "beck", {"unit": u}, stream(u)))
            running.add(u)
            free -= 1
    return commands


@units.handler("StartUnit")
def start_unit(command: Event, ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    path = ctx.project / ".beck" / "worktrees" / unit
    branch = f"beck/{ctx.iteration[:8]}/{unit}"
    base = _git(ctx.project, "rev-parse", "HEAD")
    if not path.exists():  # tolerate a crash after the worktree, before the event
        _git(ctx.project, "worktree", "add", "-q", "-b", branch, str(path), base)
    return [Result("UnitStarted", {"unit": unit, "worktree": str(path), "branch": branch, "base": base})]


# --- groups and cycles -----------------------------------------------------------------------------


@units.policy("UnitStarted", "GroupClosed", "UnitResumed")
def next_group(event: Event, ctx: Ctx) -> list[Command]:
    unit = event.payload["unit"]
    if event.type == "UnitResumed" and (_open_cycles(ctx, unit) or _implemented_before(ctx, unit)):
        return []  # the reopened cycles go on; an implemented unit went back to phase 5
    done = _groups_closed(ctx, unit)
    for g in plan(ctx)[unit]["groups"]:
        if g["id"] not in done:
            return [Command("OpenGroup", "beck", {"unit": unit, "group": g["id"]}, stream(unit))]
    return [Command("FinishUnit", "beck", {"unit": unit}, stream(unit))]


def _next_clause(ctx: Ctx, unit: str, group: str) -> str | None:
    closed = _closed_clauses(ctx, unit)
    clauses = next(g["clauses"] for g in plan(ctx)[unit]["groups"] if g["id"] == group)
    return next((c for c in clauses if c not in closed), None)


@units.handler("OpenGroup")
def open_group(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    clause = _next_clause(ctx, a["unit"], a["group"])
    results = [Result("GroupOpened", {"unit": a["unit"], "group": a["group"]})]
    if clause is None:
        return results + [Result("GroupClosed", {"unit": a["unit"], "group": a["group"]})]
    return results + [Result("CycleOpened", {"unit": a["unit"], "clause": clause, "group": a["group"], "first": True},
                             stream=cycle_stream(a["unit"], clause))]


@units.policy("CycleClosed")
def next_cycle(event: Event, ctx: Ctx) -> list[Command]:
    unit = event.payload["unit"]
    if _implemented_before(ctx, unit):  # a cycle reopened after the unit was implemented
        if _open_cycles(ctx, unit):
            return []
        return [Command("FinishUnit", "beck", {"unit": unit}, stream(unit))]
    group = _current_group(ctx, unit)
    if group is None:
        return []
    if _next_clause(ctx, unit, group) is None:
        return [Command("CloseGroup", "beck", {"unit": unit, "group": group}, stream(unit))]
    return [Command("OpenCycle", "beck", {"unit": unit, "group": group}, stream(unit))]


@units.handler("OpenCycle")
def open_cycle(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    clause = _next_clause(ctx, a["unit"], a["group"])
    assert clause is not None
    return [Result("CycleOpened", {"unit": a["unit"], "clause": clause, "group": a["group"], "first": False},
                   stream=cycle_stream(a["unit"], clause))]


@units.handler("CloseGroup")
def close_group(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("GroupClosed", {"unit": a["unit"], "group": a["group"]})]


@units.handler("FinishUnit")
def finish_unit(command: Event, ctx: Ctx) -> list[Result]:
    return [Result("UnitImplemented", {"unit": command.payload["args"]["unit"]})]


# --- parking and resolutions -----------------------------------------------------------------------


def _owner(ctx: Ctx, target: str) -> str | None:
    """The unit a change's target belongs to: a unit's id, or the unit that realizes the operation."""
    planned = plan(ctx)
    if target in planned:
        return target
    return next((u for u, p in planned.items() if target in p["realizes"]), None)


def _affected(ctx: Ctx, requester: str, target: str | None) -> list[str]:
    planned = plan(ctx)
    owner = _owner(ctx, target) if target else None
    hit = {requester} | ({owner} if owner else set())
    hit |= {u for u, p in planned.items() if owner and owner in p["depends_on"]}
    return [u for u in planned if u in hit and state(ctx, u) not in ("closed", "parked")]


@units.policy("ChangeRequested")
def park_for_a_change(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    if green_stuck(ctx, event):
        return []  # the cycle goes back to red (cycle.rewind_red): no unit waits for a resolution
    details = {k: p[k] for k in ("agent", "unit", "target", "problem", "proposal")}
    return _park_and_propose(ctx, event, _affected(ctx, p["unit"], p["target"]), p["kind"], details,
                             f"{p['kind']} change requested by {p['agent']}")


@units.policy("CycleBlocked")
def park_for_a_block(event: Event, ctx: Ctx) -> list[Command]:
    p = event.payload
    return _park_and_propose(ctx, event, _affected(ctx, p["unit"], None), "blocked_cycle", dict(p),
                             f"cycle {p['clause']} blocked: {p['reason']}")


def _park_and_propose(ctx: Ctx, event: Event, affected: list[str], kind: str, details: dict[str, Any],
                      reason: str) -> list[Command]:
    chain = event.id
    return [Command("ParkUnits", "beck", {"units": affected, "reason": reason, "because_of": chain}, "resolution"),
            _propose(chain, kind, details, None)]


def _propose(chain: str, kind: str, details: dict[str, Any], instructions: str | None) -> Command:
    context: dict[str, Any] = {"kind": kind, "details": details}
    if instructions:
        context["instructions"] = instructions
    return Command("ProposeResolution", "kent", {"session": f"resolution/{chain[:8]}", "context": context,
                                                 "chain": chain, "kind": kind, "details": details}, "resolution")


@units.handler("ParkUnits")
def park_units(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    return [Result("UnitParked", {"unit": u, "reason": a["reason"], "because_of": a["because_of"]}, stream=stream(u))
            for u in a["units"]]


@units.translator("ProposeResolution")
def resolution_proposed(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    return [Result("ResolutionProposed", dict(output["result"]), actor="kent")]


def _chain_args(ctx: Ctx, event: Event) -> dict[str, Any]:
    """The args of the ProposeResolution an event of the resolution descends from."""
    for e in reversed(ctx.store.causes(event.id)):
        if e.type == "CommandIssued" and e.payload["command"] == "ProposeResolution":
            return e.payload["args"]
    raise KeyError(f"{event.id} is not in a resolution")


@units.policy("ResolutionProposed")
def ask_approval(event: Event, ctx: Ctx) -> list[Command]:
    return [Command("ApproveResolution", "user", {"proposal": event.payload, "chain": _chain_args(ctx, event)["chain"]},
                    "resolution")]


@units.policy("ResolutionRejected")
def propose_again(event: Event, ctx: Ctx) -> list[Command]:
    a = _chain_args(ctx, event)
    return [Command("DiscardResolution", "beck", {}, "resolution"),
            _propose(a["chain"], a["kind"], a["details"], event.payload["instructions"])]


@units.handler("DiscardResolution")
def discard_resolution(command: Event, ctx: Ctx) -> list[Result]:
    _git(ctx.project, "checkout", "--", ".", ":!.beck")
    _git(ctx.project, "clean", "-fdq", "--", ".", ":!.beck")
    return []


@units.policy("ResolutionApproved")
def apply_resolution(event: Event, ctx: Ctx) -> list[Command]:
    ask = ctx.store.get(event.causation_id or "")
    return [Command("ApplyResolution", "beck", {"chain": ask.payload["args"]["chain"],
                                                "summary": ask.payload["args"]["proposal"]["summary"]}, "resolution")]


@units.handler("ApplyResolution")
def handle_apply(command: Event, ctx: Ctx) -> list[Result]:
    a = command.payload["args"]
    _git(ctx.project, "add", "-A", "--", ".", ":!.beck")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ctx.project).returncode != 0:
        _git(ctx.project, "commit", "-q", "-m", f"refactor: {a['summary']}")
    parked = [e.payload["unit"] for e in ctx.store.read(types_=("UnitParked",))
              if e.payload["because_of"] == a["chain"]]
    results = []
    replan = getattr(ctx.workspace, "replan", None)
    planned = replan() if replan else None   # the resolution may have changed the DSLs: the units' clauses with them
    if planned is not None and planned["units"] != list(plan(ctx).values()):
        results.append(Result("UnitsPlanned", planned))
    return [*results, Result("ResolutionApplied", {"base": _git(ctx.project, "rev-parse", "HEAD"), "units": parked,
                                                   "summary": a["summary"]})]


@units.handler("ResumeUnit")
def resume_unit(command: Event, ctx: Ctx) -> list[Result]:
    unit = command.payload["args"]["unit"]
    tree = ctx.workspace.worktree(unit)
    base = _git(ctx.project, "rev-parse", "HEAD")
    _git(tree, "reset", "-q", "--hard")
    _git(tree, "clean", "-fdq", "--", ".", ":!.beck")
    rebased = subprocess.run(["git", "rebase", "-q", base], cwd=tree, capture_output=True, text=True)
    if rebased.returncode != 0:
        _git(tree, "rebase", "--abort", check=False)
        return [Result("UnitParked", {"unit": unit, "reason": "the rebase on the resolution conflicted",
                                      "because_of": command.id})]
    applied = [e for e in ctx.store.read(types_=("ResolutionApplied",)) if unit in e.payload["units"]]
    reason = applied[-1].payload["summary"] if applied else "the interfaces changed"
    planned = {c for g in plan(ctx)[unit]["groups"] for c in g["clauses"]}
    dropped = [(c, g) for c, g in _open_cycles(ctx, unit) if c not in planned]
    # before UnitResumed: the policies that react to it see the dropped cycles closed
    results = [Result("CycleDropped", {"unit": unit, "clause": c, "reason": f"no longer in the domain: {reason}"},
                      stream=cycle_stream(unit, c)) for c, _ in dropped]
    results.append(Result("UnitResumed", {"unit": unit, "base": base}))
    if not [o for o in _open_cycles(ctx, unit) if o not in dropped] and _implemented_before(ctx, unit):
        results.append(Result("UnitImplemented", {"unit": unit}))
    for clause, group in _open_cycles(ctx, unit):
        if (clause, group) in dropped:
            continue
        phase = "green" if _red_committed(ctx, unit, clause) else "red"
        results.append(Result("CycleReopened", {"unit": unit, "clause": clause, "group": group, "phase": phase,
                                                "reason": f"resumed after a resolution: {reason}"},
                              stream=cycle_stream(unit, clause)))
    return results


def _open_cycles(ctx: Ctx, unit: str) -> list[tuple[str, str]]:
    opened: dict[str, str] = {}
    for e in ctx.events(types=("CycleOpened", "CycleReopened", "CycleClosed", "CycleDropped")):
        if e.payload["unit"] != unit:
            continue
        if e.type in ("CycleOpened", "CycleReopened"):
            opened[e.payload["clause"]] = e.payload["group"]
        else:
            opened.pop(e.payload["clause"], None)
    return list(opened.items())


def _implemented_before(ctx: Ctx, unit: str) -> bool:
    return bool(ctx.events(stream=stream(unit), types=("UnitImplemented",)))


def _red_committed(ctx: Ctx, unit: str, clause: str) -> bool:
    last = list(ctx.store.read(stream=cycle_stream(unit, clause), types_=("RedCommitted", "RedRewound")))
    return bool(last) and last[-1].type == "RedCommitted"


implementation = cycle.merge(units)
