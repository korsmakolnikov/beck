"""The read models the TUI and the metrics show: functions folding the events, nothing persisted."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from beck.events.store import Event, EventStore

QUIET = {"ToolCalled", "AgentTurnStarted"}


@dataclass(frozen=True)
class Tokens:
    agent: str
    turns: int
    total: int          # input + output + cache creation; cache reads apart
    cache_read: int

    @property
    def average(self) -> int:
        return self.total // self.turns if self.turns else 0


def tokens(store: EventStore) -> list[Tokens]:
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for e in store.read(types_=("AgentTurnEnded",)):
        t = e.payload["tokens"]
        row = by[e.payload["agent"]]
        row[0] += 1
        row[1] += t.get("input", 0) + t.get("output", 0) + t.get("cache_creation", 0)
        row[2] += t.get("cache_read", 0)
    return [Tokens(a, *v) for a, v in sorted(by.items())]


def total_tokens(store: EventStore) -> int:
    return sum(t.total for t in tokens(store))


def running(store: EventStore) -> list[Event]:
    """The agents' turns requested and not ended yet: who is working (or waiting for a slot) on what."""
    ended = {e.causation_id for e in store.read(types_=("AgentTurnEnded", "AgentTurnFailed"))}
    return [e for e in store.read(types_=("AgentTurnRequested",)) if e.id not in ended]


def components(store: EventStore) -> list[tuple[str, str]]:
    """Every scheduled component and its state, in the landing order."""
    scheduled = list(store.read(types_=("ExecutionDerived",)))
    if not scheduled:
        return []
    names = [u["id"] for u in scheduled[-1].payload["components"]]
    states = {u: "to do" for u in names}
    labels = {"ComponentStarted": "working", "ComponentReopened": "working", "ComponentResumed": "working",
              "ComponentParked": "parked", "ComponentImplemented": "refactoring", "ComponentReady": "ready",
              "ComponentLanded": "landed", "ComponentClosed": "done"}
    for e in store.read(types_=tuple(labels)):
        if e.payload.get("component") in states:
            states[e.payload["component"]] = labels[e.type]
    return [(u, states[u]) for u in names]


def phase(store: EventStore) -> str:
    """Where the iteration is, from its milestones."""
    milestones = [("IterationClosed", "closed"), ("BranchSquashed", "review"), ("SuiteGreen", "closing"),
                  ("ExecutionDerived", "components"), ("ArchitectureApproved", "index"), ("DomainApproved", "architecture"),
                  ("ShoppingListApproved", "domain"), ("SuiteVerified", "survey"), ("BranchCreated", "survey"),
                  ("InterviewCompleted", "branch"), ("ProjectClassified", "interview"), ("SessionStarted", "framing")]
    seen = {e.type for e in store.read(types_=tuple(m for m, _ in milestones))}
    return next((label for m, label in milestones if m in seen), "new")


def describe(e: Event) -> str:
    p: dict[str, Any] = e.payload
    if e.type in ("AgentTurnRequested", "DecisionRequested"):
        args = p["args"]
        what = args.get("component") or args.get("requirement") or ""
        return f"{p['target']} ← {p['command']} {what}".rstrip()
    if e.type == "AgentTurnEnded":
        t = p["tokens"]
        return f"{p['agent']} done ({t.get('input', 0) + t.get('output', 0)} tokens)"
    if e.type == "TestsRun":
        c = p.get("counts", {})
        return f"tests: {c.get('passed', 0)} passed, {c.get('failure', 0) + c.get('error', 0)} not ({p.get('component', '')})"
    detail = p.get("reason") or p.get("component") or p.get("requirement") or p.get("kind") or ""
    return f"{e.type} {detail}".rstrip()


def activity(store: EventStore, n: int = 40) -> list[str]:
    events = [e for e in store.read() if e.type not in QUIET]
    return [f"{e.at[11:19]} {describe(e)}" for e in events[-n:]]


def histogram(rows: list[Tokens], width: int = 30) -> list[str]:
    """A horizontal bar of the average tokens per turn of each agent."""
    top = max((r.average for r in rows), default=0) or 1
    return [f"{r.agent:<9} {'█' * max(1, r.average * width // top):<{width}} {r.average:>7} ×{r.turns}" for r in rows]


def graph(store: EventStore) -> list[str]:
    """The components in landing order with their dependencies, marked by state."""
    scheduled = list(store.read(types_=("ExecutionDerived",)))
    if not scheduled:
        return []
    deps = {u["id"]: u["depends_on"] for u in scheduled[-1].payload["components"]}
    marks = {"done": "■", "landed": "■", "ready": "▣", "refactoring": "▣", "working": "▢", "parked": "◇",
             "to do": "□"}
    return [f"{marks.get(s, '?')} {u:<20} {s:<12}" + (f" ← {', '.join(deps[u])}" if deps.get(u) else "")
            for u, s in components(store)]
