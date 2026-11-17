"""The engine: the command handler, the event store's wrapper and the router (docs/glossary.md, "The core").

    an input (an agent's output, the user's answer, a command on the socket, the session's start)
      └─ one transaction: its events, then the policies of every new event, in order
           a policy's command for beck: its handler runs at once, its events join the queue
           a policy's command for an agent: AgentTurnRequested (the agent is a side effect)
           a policy's command for the user: DecisionRequested
      └─ committed: the after-commit hooks (the read models, the dashboard's signal), then the requested
         turns start (AgentTurnStarted: the input and the folder's snapshot) as slots free up

* Commands are never stored: only events, each with what its replay needs. A policy runs once, in the
  transaction of its event; it reads the state as at its event (`Ctx.upto`), the scheduler alone the
  present (`component.schedule`).
* A crash in a policy, a handler or a translator aborts the input's transaction: the input's own facts (a
  turn's output, the user's answer) are recorded with a BeckCrashed, and nothing else. `beck attach`,
  after the fix, processes them again: the agent is not asked again.
* Recovery rebuilds the state from the events and runs no policy again: a requested turn not started
  starts; a turn started that never ended (beck died while the agent worked) gets its folder put back to
  the turn's snapshot and the same input again; an input whose processing crashed is processed again.
* An agent's turn gets $BECK_CAUSE = its AgentTurnRequested: what it sends on the socket (`beck send`)
  joins the chain.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import subprocess
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from beck.agents import protocol
from beck.events import log
from beck.events.store import Event, EventStore
from beck.machine.signatures import blocked, signature

logger = logging.getLogger("beck.machine")


@dataclass(frozen=True)
class Command:
    name: str
    target: str                 # beck | kent | unclebob | martin | reader | user
    args: dict[str, Any]
    stream: str


@dataclass(frozen=True)
class Result:
    """An event an executor wants to append, caused by its command."""
    type: str
    payload: dict[str, Any]
    actor: str = "beck"
    tokens: dict[str, int] | None = None
    stream: str | None = None   # default: the command's stream


@dataclass(frozen=True)
class Reply:
    """What a handler answers a command from outside with: its events, and a value for the client (the
    summary an agent reads)."""
    results: list[Result]
    value: dict[str, Any]


@dataclass(frozen=True)
class Turn:
    output: Any                 # the agent's JSON, not checked yet
    model: str
    tokens: dict[str, int]
    session: str | None = None                                        # the CLI's own session id
    tools: list[dict[str, Any]] = field(default_factory=list)        # the tool calls: {"tool", "input"}
    definition: str | None = None                                     # the hash of the agent's definition


class TurnFailed(RuntimeError):
    """The CLI could not complete the turn (it crashed, timed out, answered nothing)."""


class Agents(Protocol):
    async def turn(self, agent: str, session: str, envelope: dict[str, Any], env: dict[str, str],
                   cwd: Path) -> Turn: ...


Policy = Callable[[Event, "Ctx"], list[Command]]
Handler = Callable[["Issued", "Ctx"], "list[Result] | Reply"]
Translator = Callable[[Event, dict[str, Any], "Ctx"], list[Result]]


@dataclass
class Machine:
    """The policies of a machine by event type, its handlers and translators by command."""
    policies: dict[str, list[tuple[str, Policy]]] = field(default_factory=dict)
    handlers: dict[str, Handler] = field(default_factory=dict)
    translators: dict[str, Translator] = field(default_factory=dict)

    def policy(self, *types: str) -> Callable[[Policy], Policy]:
        def register(fn: Policy) -> Policy:
            for t in types:
                self.policies.setdefault(t, []).append((fn.__name__, fn))
            return fn
        return register

    def handler(self, command: str) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            self.handlers[command] = fn
            return fn
        return register

    def translator(self, command: str) -> Callable[[Translator], Translator]:
        def register(fn: Translator) -> Translator:
            self.translators[command] = fn
            return fn
        return register

    def merge(self, other: Machine) -> Machine:
        merged = Machine({k: list(v) for k, v in self.policies.items()}, dict(self.handlers), dict(self.translators))
        for t, ps in other.policies.items():
            merged.policies.setdefault(t, []).extend(ps)
        merged.handlers.update(other.handlers)
        merged.translators.update(other.translators)
        return merged


@dataclass
class Ctx:
    """What policies and handlers read: the store, as it was at an event, and the workspace."""
    store: EventStore
    project: Path                       # where .beck/ is
    iteration: str
    workspace: Any                      # worktrees, scopes, unit contexts (beck.machine.workspace)
    limits: tuple[int, int] = (3, 30)   # the repetition rule: times, attempts
    components_in_parallel: int = 4
    upto: int | None = None             # policies see the events up to their own
    flakiness: tuple[int, int] = (3, 5)  # the flakiness detector: the window, the runs of each hunt

    def events(self, *, stream: str | None = None, types: tuple[str, ...] = ()) -> list[Event]:
        events = self.store.read(stream=stream, types_=types)
        return [e for e in events if self.upto is None or e.seq <= self.upto]


conversation = Machine()


@conversation.policy("QuestionAsked")
def ask_the_user(event: Event, ctx: Ctx) -> list[Command]:
    """An agent's question goes to the user; the turn it came from waits."""
    return [Command("Ask", "user", {"question": event.payload["question"], "turn": event.causation_id},
                    event.stream)]


def action(id_: str, does: str, **args: str) -> dict[str, Any]:
    """One action of a routed question's menu: what it does, and the arguments it needs."""
    return {"id": id_, "does": does, "args": args}


def ask_routed(text: str, why: str, situation: str, actions: list[dict[str, Any]], stream: str, turn: str) -> Command:
    """beck's own question: the user answers in words, kent turns them into one action of the menu.

    The machine that asked acts on the DecisionRouted whose `situation` is its own.
    """
    return Command("Ask", "user", {"question": {"text": text, "why": why}, "turn": turn,
                                   "route": {"situation": situation, "actions": actions}}, stream)


@conversation.policy("QuestionAnswered")
def answer_the_agent(event: Event, ctx: Ctx) -> list[Command]:
    """The answer, wrapped, goes back to the session that asked, for the same command."""
    ask = ctx.store.get(event.causation_id or "")
    turn = ctx.store.get(ask.payload["args"]["turn"])
    if turn.type != "AgentTurnRequested":  # beck's own question: kent routes the words to an action of its menu
        route = ask.payload["args"].get("route")
        return [_route_turn(ask, event.payload["text"], None)] if route else []
    args = {k: v for k, v in turn.payload["args"].items() if k not in ("context", "retry", "answer")}
    args["answer"] = {"question": ask.payload["args"]["question"], "text": event.payload["text"]}
    return [Command(turn.payload["command"], turn.payload["target"], args, turn.stream)]


def _route_turn(ask: Event, words: str, retry: tuple[int, str] | None) -> Command:
    route = ask.payload["args"]["route"]
    context = {"situation": route["situation"], "question": ask.payload["args"]["question"]["text"], "answer": words,
               "actions": route["actions"]}
    args: dict[str, Any] = {"session": f"decision/{ask.id[:8]}", "context": context, "ask": ask.id}
    if retry:
        args["retry"] = list(retry)
    return Command("RouteDecision", "kent", args, ask.stream)


@conversation.translator("RouteDecision")
def decision_routed(command: Event, output: dict[str, Any], ctx: Ctx) -> list[Result]:
    ask = ctx.store.get(command.payload["args"]["ask"])
    route = ask.payload["args"]["route"]
    r = output["result"]
    menu = {a["id"]: a for a in route["actions"]}
    problems = []
    if r["action"] not in menu:
        problems.append(f"{r['action']!r} is not in the menu: {', '.join(menu)}")
    else:
        missing = sorted(set(menu[r["action"]]["args"]) - set(r["args"]))
        extra = sorted(set(r["args"]) - set(menu[r["action"]]["args"]))
        problems += [f"missing argument {m!r}" for m in missing] + [f"unknown argument {x!r}" for x in extra]
    if problems:
        return [Result("RouteRejected", {"reason": "; ".join(problems), "signature": signature(problems)},
                       actor="kent")]
    return [Result("DecisionRouted", {"situation": route["situation"], "action": r["action"], "args": r["args"],
                                      "why": r["why"], "ask": ask.id}, actor="kent")]


@conversation.policy("RouteRejected")
def route_again(event: Event, ctx: Ctx) -> list[Command]:
    """kent again with the reason; when its answers repeat, the user is asked again."""
    turn = ctx.store.get(event.causation_id or "")
    ask = ctx.store.get(turn.payload["args"]["ask"])
    rejected = [e.payload["signature"] for e in ctx.events(stream=event.stream, types=("RouteRejected",))
                if ctx.store.get(e.causation_id or "").payload["args"].get("ask") == ask.id]
    if blocked(rejected, *ctx.limits):
        return [Command("Ask", "user", dict(ask.payload["args"]), ask.stream)]
    words = turn.payload["args"]["context"]["answer"] if "context" in turn.payload["args"] else ""
    return [_route_turn(ask, words, (len(rejected) + 1, event.payload["reason"]))]


GO_ON = {"SuiteVerified", "EnvironmentUp", "SuiteGreen", "StaticChecksPassed", "ProjectScaffolded"}


@conversation.handler("GoOn")
def handle_go_on(command: Issued, ctx: Ctx) -> list[Result]:
    """The user's decision to go on as if the step had passed: the step's own event, as the user's."""
    a = command.payload["args"]
    if a["as"] not in GO_ON:
        raise ValueError(f"GoOn cannot stand for {a['as']}")
    return [Result(a["as"], dict(a.get("payload", {})), actor="user")]


@conversation.policy("AgentTurnFailed")
def retry_a_failed_turn(event: Event, ctx: Ctx) -> list[Command]:
    """The same command again; the same failure three times in a row asks the user."""
    turn = ctx.store.get(event.causation_id or "")
    failures = [e.payload["signature"] for e in ctx.events(stream=event.stream, types=("AgentTurnFailed",))
                if e.payload["command"] == event.payload["command"]]
    why = blocked(failures, *ctx.limits)
    if why:
        actions = [action("retry", "Run the agent's turn again, as it was.")]
        if turn.payload["args"].get("component"):
            actions.append(action("park-component", "Park the turn's component; the other components go on."))
        return [ask_routed(f"{event.payload['agent']}'s {event.payload['command']} fails ({why}): "
                           f"{event.payload['error']}", "The CLI cannot complete the turn.", "turn-failed", actions,
                           event.stream, event.id)]
    return [Command(turn.payload["command"], turn.payload["target"], dict(turn.payload["args"]), turn.stream)]


# the machines that retry these themselves, under their own loop rule (beck.machine.cycle, beck.machine.refactor);
# FixStaticFindings of the feature branch (args.branch) is retried here
OWN_RETRIES = {"WriteRedTest", "WriteGreenCode", "CoverLines", "RefactorComponent", "ReviewComponent", "ResolveConflict",
               "FixStaticFindings"}


@conversation.policy("SignalRejected")
def retry_a_rejected_turn(event: Event, ctx: Ctx) -> list[Command]:
    """A malformed answer outside the cycles: the same command again, with the errors as the reason; the same
    rejection three times in a row asks the user, as a turn that fails does."""
    name = event.payload["command"]
    turn = ctx.store.get(event.causation_id or "")
    own = name in OWN_RETRIES and not (name == "FixStaticFindings" and turn.payload["args"].get("branch"))
    if own or "context" not in turn.payload["args"]:
        return []
    rejected = [e.payload["signature"] for e in ctx.events(stream=event.stream, types=("SignalRejected",))
                if e.payload["command"] == name]
    why = blocked(rejected, *ctx.limits)
    if why:
        actions = [action("retry", "Run the agent's turn again, as it was.")]
        if turn.payload["args"].get("component"):
            actions.append(action("park-component", "Park the turn's component; the other components go on."))
        return [ask_routed(f"{event.payload['agent']}'s {name} answers wrong ({why}): "
                           f"{'; '.join(event.payload['errors'])[:500]}", "The answer does not match its schema.",
                           "turn-failed", actions, event.stream, event.id)]
    args = {**turn.payload["args"], "retry": [len(rejected) + 1, "; ".join(event.payload["errors"])[:2000]]}
    return [Command(name, turn.payload["target"], args, turn.stream)]


@conversation.policy("DecisionRouted")
def after_turn_failed(event: Event, ctx: Ctx) -> list[Command]:
    if event.payload["situation"] != "turn-failed":
        return []
    failed = ctx.store.get(ctx.store.get(event.payload["ask"]).payload["args"]["turn"])
    turn = ctx.store.get(failed.causation_id or "")
    if event.payload["action"] == "retry":
        return [Command(turn.payload["command"], turn.payload["target"], dict(turn.payload["args"]), turn.stream)]
    component = turn.payload["args"]["component"]
    return [Command("ParkComponents", "beck", {"components": [component], "reason": f"the user parked it: {event.payload['why']}",
                                          "because_of": event.id}, f"component:{component}")]


def snapshot(folder: Path) -> str | None:
    """The tree of a folder's files as git sees them (tracked and untracked, ignored left out), without touching its
    index; None outside a repository."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        add = subprocess.run(["git", "add", "-A", "."], cwd=folder, env=env, capture_output=True)
        if add.returncode != 0:
            return None
        tree = subprocess.run(["git", "write-tree"], cwd=folder, env=env, capture_output=True, text=True)
        return tree.stdout.strip() if tree.returncode == 0 else None


def changes(folder: Path, before: str | None) -> str:
    """The binary patch from a snapshot to the folder now: what a turn changed."""
    after = snapshot(folder) if before else None
    if not before or not after or before == after:
        return ""
    diff = subprocess.run(["git", "diff", "--binary", before, after], cwd=folder, capture_output=True, text=True)
    return diff.stdout if diff.returncode == 0 else ""


def crash(where: str, name: str, error: BaseException) -> Result:
    """beck's own exception, as an event: traceable, and the user resumes after the fix."""
    text = f"{type(error).__name__}: {error}"[:2000]
    trace = "".join(traceback.format_exception(error))[-4000:]
    return Result("BeckCrashed", {"where": where, "name": name, "error": text, "traceback": trace,
                                  "signature": signature([where, name, text])})


REQUESTS = ("AgentTurnRequested", "DecisionRequested")    # beck asked an agent, or the user
TURN_FACTS = {"AgentTurnStarted", "ToolCalled", "AgentTurnEnded", "TurnChanged", "AgentTurnFailed"}


@dataclass(frozen=True)
class Issued:
    """A command for beck as its handler reads it: never stored. `id` is the event it answers (its results'
    cause), so a handler's `because_of: command.id` names that event."""
    payload: dict[str, Any]             # command, target, args
    stream: str
    cause: Event

    @property
    def id(self) -> str:
        return self.cause.id

    @property
    def correlation_id(self) -> str:
        return self.cause.correlation_id

    @property
    def causation_id(self) -> str:
        return self.cause.id


def request_of(store: EventStore, event: Event, kind: str = "AgentTurnRequested") -> Event:
    """The request an event descends from: the agent's turn (or the user's decision) its chain of causes
    starts from, the nearest. A verifier's verdict answers the declaration, which answers the turn."""
    current: Event | None = event
    while current is not None:
        if current.type == kind:
            return current
        current = store.get(current.causation_id) if current.causation_id else None
    raise KeyError(f"{event.type} {event.id} descends from no {kind}")


class _Aborted(Exception):
    """An input's processing crashed: its transaction is rolled back, the crash recorded with its facts."""

    def __init__(self, crashed: Result):
        super().__init__(crashed.payload["error"])
        self.crashed = crashed


def opened(store: EventStore, request: Event) -> bool:
    """Whether a request still waits for the outside: a turn with no outcome, a decision with no answer. (One
    whose processing crashed waits for the fix: `beck attach` processes it again, nobody answers it twice.)"""
    effects = store.caused_by(request.id)
    if request.type == "AgentTurnRequested":
        return not any(e.type in ("AgentTurnEnded", "AgentTurnFailed") for e in effects)
    return not any(e.actor == "user" for e in effects)


class Engine:
    """Runs the machine on the event store (the module's doc). beck's own exceptions never end the session:
    each is a BeckCrashed, its input is not processed again in this run, and `beck attach` resumes it."""

    def __init__(self, ctx: Ctx, machine: Machine, agents: Agents, slots: int = 4,
                 after_commit: list[Callable[[list[Event]], object]] | None = None):
        from beck.machine.tools import tools  # the commands the agents send (beck send)
        self.ctx, self.machine, self.agents, self.slots = ctx, conversation.merge(tools).merge(machine), agents, slots
        self.after_commit = after_commit or []  # read models and signals: they never stop the session
        last = ctx.store.last()
        self._cursor = last.seq if last else 0  # the policies have run on everything before
        self._running: dict[str, asyncio.Task[list[Result]]] = {}
        self.crashes: list[Event] = []          # this run's BeckCrashed
        self._crashed: set[str] = set()         # the requests whose processing crashed in this run
        self._recovered = False
        from beck import socket  # what a client of this process sends the socket reaches the engine directly
        socket._LOCAL[Path(ctx.project).resolve()] = self.serve

    # --- the store's wrapper ---

    def _process(self, cause: Event | None, facts: list[Result], then: Callable[[], list[Result]] | None = None,
                 again: list[Event] | None = None, kind: str = "input", name: str = "") -> bool:
        """One input in one transaction: its facts, what `then` makes of them, every policy of the new events
        (and of `again`, events recorded before whose processing crashed). False when it crashed."""
        store = self.ctx.store
        cursor = self._cursor
        stream = cause.stream if cause is not None else "session"
        try:
            with store.transaction():
                for r in facts:
                    self._append(r, cause, None, stream)
                for e in again or []:
                    self._policies(e)
                if then is not None:
                    for r in self._guard("translator", name or kind, then):
                        self._append(r, cause, None, stream)
                self._react()
        except _Aborted as aborted:
            self._cursor = cursor
            on = None if kind == "command" else cause   # a command's crash leaves its agent's turn as it was
            with store.transaction():
                for r in facts:   # the input's own facts: processed again after the fix
                    self._append(r, cause, None, stream)
                crash_event = self._append(Result(aborted.crashed.type, {**aborted.crashed.payload, "input": kind}),
                                           on, None, stream)
            self.crashes.append(crash_event)
            if on is not None:
                self._crashed.add(on.id)
            last = store.last()
            self._cursor = last.seq if last else 0   # the recorded facts wait for the fix: no policy on them now
            return False
        new = list(store.read(after=cursor))
        for hook in self.after_commit:
            try:
                hook(new)
            except Exception:
                logger.exception("an after-commit hook crashed: what it feeds can be rebuilt from the events")
        return True

    def _guard(self, where: str, name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except _Aborted:
            raise
        except Exception as error:
            logger.exception("%s %s crashed", where, name)
            raise _Aborted(crash(where, name, error)) from error

    def _append(self, r: Result, cause: Event | None, policy: str | None, stream: str) -> Event:
        return self.ctx.store.append(r.type, r.payload, stream=r.stream or stream, actor=r.actor, cause=cause,
                                     tokens=r.tokens, policy=policy)

    # --- the router ---

    def _react(self) -> None:
        """The policies of every event after the cursor, in order, the commands they issue executed at once."""
        while True:
            events = list(self.ctx.store.read(after=self._cursor))
            if not events:
                return
            for event in events:
                self._cursor = event.seq
                self._policies(event)

    def _policies(self, event: Event) -> None:
        for name, policy in self.machine.policies.get(event.type, []):
            at = Ctx(**{**self.ctx.__dict__, "upto": event.seq})
            with log.about(event):
                commands: list[Command] = self._guard("policy", name, functools.partial(policy, event, at))
            for c in commands:
                self._submit(c, event, name)

    # --- the command handler ---

    def _submit(self, command: Command, cause: Event, policy: str) -> None:
        payload = {"command": command.name, "target": command.target, "args": command.args}
        if command.target == "beck":
            handler = self.machine.handlers.get(command.name)
            if handler is None:
                raise _Aborted(crash("handler", command.name, KeyError(f"no handler for {command.name}")))
            issued = Issued(payload, command.stream, cause)
            with log.about(cause):
                results = self._guard("handler", command.name, lambda: handler(issued, self.ctx))  # type: ignore[arg-type]
            logger.info("%s ran %s", policy, command.name)
            for r in results:
                self._append(r, cause, policy, command.stream)
            return
        kind = "DecisionRequested" if command.target == "user" else "AgentTurnRequested"
        self._append(Result(kind, payload), cause, policy, command.stream)
        logger.info("%s asked %s for %s", policy, command.target, command.name)

    def command(self, name: str, args: dict[str, Any], cause_id: str, actor: str) -> dict[str, Any]:
        """A command from outside (the socket: an agent's `beck send`): its handler, and the policies of its
        events, in one transaction. The handler's answer for the client, or the crash."""
        store = self.ctx.store
        cause = store.get(cause_id)
        handler = self.machine.handlers.get(name)
        if handler is None:
            raise KeyError(f"no handler for {name}")
        issued = Issued({"command": name, "target": "beck", "args": {**args, "actor": actor}}, cause.stream, cause)
        answer: dict[str, Any] = {}

        def run() -> list[Result]:
            out = handler(issued, self.ctx)  # type: ignore[arg-type]
            if isinstance(out, Reply):
                answer.update(out.value)
                return out.results
            return out
        if not self._process(cause, [], run, kind="command", name=name):
            return {"ok": False, "error": self.crashes[-1].payload["error"]}
        return {"ok": True, **answer}

    def serve(self, message: dict[str, Any]) -> dict[str, Any]:
        """The socket's requests: an agent's `beck send test` or `beck send check`."""
        names = {"test": "RunTests", "check": "RunChecks"}
        if message.get("command") not in names:
            return {"ok": False, "error": f"unknown command {message.get('command')!r}: {', '.join(names)}"}
        return self.command(names[message["command"]], message.get("args", {}), message["cause"],
                            message.get("actor", "user"))

    def record(self, type_: str, payload: dict[str, Any], *, stream: str = "session", actor: str = "beck",
               cause: Event | None = None) -> Event:
        """A fact from outside, as an input: recorded, and every policy it sets off, in one transaction."""
        before = self.ctx.store.last()
        self._process(cause, [Result(type_, payload, actor=actor, stream=stream)], kind="input")
        return next(e for e in self.ctx.store.read(after=before.seq if before else 0) if e.type == type_)

    # --- the requests ---

    def pending(self) -> list[Event]:
        """The requests still waiting: the agents' turns and the user's decisions."""
        store = self.ctx.store
        return [r for r in store.read(types_=REQUESTS) if opened(store, r)]

    def answer(self, request_id: str, results: list[Result]) -> None:
        """The user's answer to a pending decision (a question, an approval), and all it causes."""
        request = self.ctx.store.get(request_id)
        if request.type != "DecisionRequested" or not opened(self.ctx.store, request):
            raise ValueError(f"{request_id} is not a pending decision of the user")
        self._process(request, results, kind="answer", name=request.payload["command"])   # the answer: a fact

    async def _turn(self, request: Event, started: Event) -> list[Result]:
        """The agent's turn: its facts (tools, its output and its tokens, what it changed), not yet judged."""
        agent, name = request.payload["target"], request.payload["command"]
        envelope, cwd = started.payload["envelope"], Path(started.payload["folder"])
        env = {"BECK_CAUSE": request.id, "BECK_ITERATION": self.ctx.iteration, "BECK_ACTOR": agent,
               "BECK_PROJECT": str(self.ctx.project)}
        before = started.payload.get("snapshot")
        clock = time.monotonic()
        try:
            turn = await self.agents.turn(agent, request.payload["args"]["session"], envelope, env, cwd)
        except TurnFailed as error:
            text = str(error)[:2000] or "the turn failed"
            patch = await asyncio.to_thread(changes, cwd, before)   # a failed turn may have changed files too
            return [*([Result("TurnChanged", {"agent": agent, "patch": patch}, actor=agent)] if patch else []),
                    Result("AgentTurnFailed", {"agent": agent, "command": name, "error": text,
                                               "signature": signature([text])}, actor=agent)]
        tools = [Result("ToolCalled", {"agent": agent, "tool": t["tool"], "input": t["input"]}, actor=agent)
                 for t in turn.tools]
        ended = Result("AgentTurnEnded", {"agent": agent, "model": turn.model, "tokens": turn.tokens,
                                          "output": turn.output,
                                          "duration_ms": int((time.monotonic() - clock) * 1000),
                                          **({"session": turn.session} if turn.session else {}),
                                          **({"definition": turn.definition} if turn.definition else {})},
                       actor=agent, tokens=turn.tokens)
        patch = await asyncio.to_thread(changes, cwd, before)
        return [*tools, ended, *([Result("TurnChanged", {"agent": agent, "patch": patch}, actor=agent)] if patch else [])]

    def _judge(self, request: Event, output: Any) -> list[Result]:
        """An agent's output, distilled: rejected, a question, or what its translator makes of it."""
        agent, name = request.payload["target"], request.payload["command"]
        checked = protocol.check_output(agent, name, output)
        if checked.output is None:
            return [Result("SignalRejected", {"agent": agent, "command": name, "errors": checked.errors,
                                              "signature": signature(checked.errors)}, actor=agent)]
        if checked.output["outcome"] == "question":
            return [Result("QuestionAsked", {"agent": agent, "command": name,
                                             "question": checked.output["question"]}, actor=agent)]
        return self.machine.translators[name](request, checked.output, self.ctx)

    def _start(self, request: Event, again: bool) -> Event:
        """AgentTurnStarted: the input the agent gets and its folder's snapshot; a turn started again first
        gets its folder back to the snapshot of its first start."""
        agent, name, args = request.payload["target"], request.payload["command"], request.payload["args"]
        cwd = self.ctx.workspace.worktree(args["component"]) if args.get("component") and agent != "kent" \
            else self.ctx.project
        earlier = [e for e in self.ctx.store.caused_by(request.id) if e.type == "AgentTurnStarted"]
        if again and earlier and earlier[0].payload.get("snapshot"):
            restore(Path(earlier[0].payload["folder"]), earlier[0].payload["snapshot"])
        if "answer" in args:
            envelope = protocol.answer(agent, name, request.id, request.correlation_id, args["answer"]["question"],
                                       args["answer"]["text"])
        else:
            envelope = protocol.envelope(agent, name, request.id, request.correlation_id, args["context"],
                                         tuple(args["retry"]) if args.get("retry") else None)  # type: ignore[arg-type]
        snap = snapshot(Path(cwd))
        payload: dict[str, Any] = {"agent": agent, "command": name, "envelope": envelope, "folder": str(cwd),
                                   **({"snapshot": snap} if snap else {}), **({"again": True} if again else {})}
        with self.ctx.store.transaction():
            started = self._append(Result("AgentTurnStarted", payload, actor="beck"), request, None, request.stream)
        self._cursor = max(self._cursor, started.seq)
        return started

    # --- recovery ---

    def _recover(self) -> None:
        """The state rebuilt from the events, the work in flight resumed (the module's doc)."""
        store = self.ctx.store
        for root in store.read(types_=("SessionStarted",)):
            if not [e for e in store.caused_by(root.id) if e.type != "BeckCrashed"] and root.id not in self._crashed:
                self._process(None, [], again=[root], kind="start")
        for request in list(store.read(types_=REQUESTS)):
            effects = store.caused_by(request.id)
            if not effects or effects[-1].type != "BeckCrashed" or request.id in self._crashed:
                continue
            facts = [e for e in effects if e.type not in ("BeckCrashed", "AgentTurnStarted")]
            if request.type == "AgentTurnRequested":
                ended = next((e for e in reversed(facts) if e.type == "AgentTurnEnded"), None)
                if ended is None and not any(e.type == "AgentTurnFailed" for e in facts):
                    continue   # crashed before it ran: the launcher starts it
                judged = (lambda r=request, o=ended.payload["output"]: self._judge(r, o)) if ended else None
                self._process(request, [], judged, again=facts, kind="turn", name=request.payload["command"])
            else:
                self._process(request, [], again=facts, kind="answer")

    def _in_flight(self, request: Event) -> bool:
        effects = self.ctx.store.caused_by(request.id)
        return any(e.type == "AgentTurnStarted" for e in effects) and not any(
            e.type in ("AgentTurnEnded", "AgentTurnFailed") for e in effects)

    async def run_until_idle(self) -> None:
        """Processes and runs the turns until nothing is left but the user's answers."""
        if not self._recovered:
            self._recovered = True
            self._recover()
        while True:
            for request in self.pending():
                if request.type != "AgentTurnRequested" or request.id in self._running or request.id in self._crashed:
                    continue
                if len(self._running) >= self.slots:
                    break
                started = self._start(request, again=self._in_flight(request))
                self._running[request.id] = asyncio.create_task(self._turn(request, started))
            if not self._running:
                return
            done, _ = await asyncio.wait(self._running.values(), return_when=asyncio.FIRST_COMPLETED)
            for rid, task in list(self._running.items()):
                if task not in done:
                    continue
                del self._running[rid]
                request = self.ctx.store.get(rid)
                try:
                    facts = task.result()
                except Exception as error:
                    logger.exception("turn %s crashed", request.payload["command"])
                    self._process(request, [], functools.partial(self._raise, "turn", request, error), kind="turn",
                                  name=request.payload["command"])
                    continue
                ended = next((r for r in facts if r.type == "AgentTurnEnded"), None)
                judged = (lambda r=request, o=ended.payload["output"]: self._judge(r, o)) if ended else None
                self._process(request, facts, judged, kind="turn", name=request.payload["command"])

    @staticmethod
    def _raise(where: str, request: Event, error: BaseException) -> list[Result]:
        raise _Aborted(crash(where, request.payload["command"], error))


def restore(folder: Path, tree: str) -> None:
    """The folder's files put back as the snapshot holds them: the tracked and untracked files of the tree,
    and nothing else (the ignored files are left alone)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        now = subprocess.run(["git", "add", "-A", "."], cwd=folder, env=env, capture_output=True)
        if now.returncode != 0:
            return
        present = set(subprocess.run(["git", "ls-files"], cwd=folder, env=env, capture_output=True,
                                     text=True).stdout.splitlines())
        subprocess.run(["git", "read-tree", tree], cwd=folder, env=env, check=True, capture_output=True)
        kept = set(subprocess.run(["git", "ls-files"], cwd=folder, env=env, capture_output=True,
                                  text=True).stdout.splitlines())
        for path in present - kept:
            (folder / path).unlink(missing_ok=True)
        subprocess.run(["git", "checkout-index", "-a", "-f"], cwd=folder, env=env, check=True, capture_output=True)
