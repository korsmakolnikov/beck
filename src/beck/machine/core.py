"""The reactor: policies turn events into commands, executors turn commands into events.

    event E ──policy P──► CommandIssued (cause E, policy P)
                             │  beck: a deterministic handler acts at once
                             │  an agent: a turn, JSON in and JSON out
                             │  the user: waits for an answer
                             ▼
                  result events + CommandDone (cause: the CommandIssued), in one transaction

* A policy never reacts twice to the same event: the CommandIssued it wrote
  is the anchor (`store.issued`). A policy reads the state as it was at its
  event (`Ctx.upto`), so a replay issues exactly what the first run did;
  the scheduler alone reads the present (`unit.schedule`).
* A command without its CommandDone is pending: `run_until_idle` executes
  it, also after a Ctrl-C (`beck attach`). Handlers must tolerate running
  again after a crash between their side effect and their events.
* An agent's turn gets $BECK_CAUSE = the CommandIssued id: the `beck test`
  runs of the turn join the chain.
"""

from __future__ import annotations

import asyncio
import logging
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
class Turn:
    output: Any                 # the agent's JSON, not checked yet
    model: str
    tokens: dict[str, int]
    session: str | None = None                                        # the CLI's own session id
    tools: list[dict[str, Any]] = field(default_factory=list)        # the tool calls: {"tool", "input"}


class TurnFailed(RuntimeError):
    """The CLI could not complete the turn (it crashed, timed out, answered nothing)."""


class Agents(Protocol):
    async def turn(self, agent: str, session: str, envelope: dict[str, Any], env: dict[str, str],
                   cwd: Path) -> Turn: ...


Policy = Callable[[Event, "Ctx"], list[Command]]
Handler = Callable[[Event, "Ctx"], list[Result]]
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
    workspace: Any                      # worktrees, scopes, group contexts (beck.machine.workspace)
    limits: tuple[int, int] = (3, 30)   # the repetition rule: times, attempts
    units_in_parallel: int = 4
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
    if turn.type != "CommandIssued":  # beck's own question: kent routes the words to an action of its menu
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
def handle_go_on(command: Event, ctx: Ctx) -> list[Result]:
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
        if turn.payload["args"].get("unit"):
            actions.append(action("park-unit", "Park the turn's unit; the other units go on."))
        return [ask_routed(f"{event.payload['agent']}'s {event.payload['command']} fails ({why}): "
                           f"{event.payload['error']}", "The CLI cannot complete the turn.", "turn-failed", actions,
                           event.stream, event.id)]
    return [Command(turn.payload["command"], turn.payload["target"], dict(turn.payload["args"]), turn.stream)]


# the machines that retry these themselves, under their own loop rule (beck.machine.cycle, beck.machine.refactor);
# FixStaticFindings of the feature branch (args.branch) is retried here
OWN_RETRIES = {"WriteRedTest", "WriteGreenCode", "CoverLines", "RefactorUnit", "ReviewUnit", "ResolveConflict",
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
    later = [e for e in ctx.store.read(stream=event.stream, types_=("CommandIssued",))
             if e.seq > event.seq and e.payload["command"] == name]
    unit = turn.payload["args"].get("unit")
    closed = bool(unit) and bool(list(ctx.store.read(stream=f"unit:{unit}", types_=("UnitClosed",))))
    if later or closed:   # stale: a later turn took it over, or its unit closed (an attach replays old rejections)
        return []
    rejected = [e.payload["signature"] for e in ctx.events(stream=event.stream, types=("SignalRejected",))
                if e.payload["command"] == name]
    why = blocked(rejected, *ctx.limits)
    if why:
        actions = [action("retry", "Run the agent's turn again, as it was.")]
        if turn.payload["args"].get("unit"):
            actions.append(action("park-unit", "Park the turn's unit; the other units go on."))
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
    unit = turn.payload["args"]["unit"]
    return [Command("ParkUnits", "beck", {"units": [unit], "reason": f"the user parked it: {event.payload['why']}",
                                          "because_of": event.id}, f"unit:{unit}")]


def crash(where: str, name: str, error: BaseException) -> Result:
    """beck's own exception, as an event: smart_audit sends it to argyris, the user resumes after the fix."""
    text = f"{type(error).__name__}: {error}"[:2000]
    trace = "".join(traceback.format_exception(error))[-4000:]
    return Result("BeckCrashed", {"where": where, "name": name, "error": text, "traceback": trace,
                                  "signature": signature([where, name, text])})


class _Crashed(Exception):
    """A turn whose translator raised: the turn's results, its tokens kept, and the crash, without CommandDone."""

    def __init__(self, results: list[Result]):
        super().__init__()
        self.results = results


class Reactor:
    """Runs the machine. beck's own exceptions never end the session: each is recorded as BeckCrashed and
    is not tried again in this run; the command stays pending (the policy unanchored), so `beck attach`,
    after the fix, runs it again."""

    def __init__(self, ctx: Ctx, machine: Machine, agents: Agents, slots: int = 4):
        self.ctx, self.machine, self.agents, self.slots = ctx, conversation.merge(machine), agents, slots
        self._cursor = 0
        self._running: dict[str, asyncio.Task[list[Result]]] = {}
        self.crashes: list[Event] = []          # this run's BeckCrashed
        self._crashed: set[str] = set()         # the commands that crashed in this run

    # --- policies ---

    def react(self) -> bool:
        """Runs the policies on the events after the cursor; True when it issued something (or recorded a crash)."""
        issued = False
        store = self.ctx.store
        for event in list(store.read(after=self._cursor)):
            self._cursor = event.seq
            for name, policy in self.machine.policies.get(event.type, []):
                if store.issued(event.id, name):
                    continue
                with log.about(event):
                    try:
                        commands = policy(event, Ctx(**{**self.ctx.__dict__, "upto": event.seq}))
                    except Exception as error:
                        logger.exception("policy %s crashed on %s", name, event.type)
                        r = crash("policy", name, error)
                        self.crashes.append(store.append(r.type, r.payload, stream=event.stream, cause=event))
                        issued = True           # an event to react to: smart_audit posts it
                        continue
                    for c in commands:
                        store.append("CommandIssued", {"command": c.name, "target": c.target, "args": c.args},
                                     stream=c.stream, cause=event, policy=name)
                        logger.info("%s issued %s for %s", name, c.name, c.target)
                        issued = True
        return issued

    # --- commands ---

    def pending(self) -> list[Event]:
        done = {e.causation_id for e in self.ctx.store.read(types_=("CommandDone",))}
        return [e for e in self.ctx.store.read(types_=("CommandIssued",)) if e.id not in done]

    def _finish(self, command: Event, results: list[Result]) -> None:
        store = self.ctx.store
        with store.transaction():
            for r in results:
                store.append(r.type, r.payload, stream=r.stream or command.stream, actor=r.actor, cause=command,
                             tokens=r.tokens)
            store.append("CommandDone", {"command": command.payload["command"]}, stream=command.stream,
                         cause=command)

    def _execute(self, command: Event) -> list[Result]:
        name = command.payload["command"]
        handler = self.machine.handlers.get(name)
        if handler is None:
            raise KeyError(f"no handler for {name}")
        with log.about(command):
            return handler(command, self.ctx)

    async def _turn(self, command: Event) -> list[Result]:
        agent, name, args = command.payload["target"], command.payload["command"], command.payload["args"]
        if "answer" in args:
            envelope = protocol.answer(agent, name, command.id, command.correlation_id, args["answer"]["question"],
                                       args["answer"]["text"])
        else:
            envelope = protocol.envelope(agent, name, command.id, command.correlation_id, args["context"],
                                         tuple(args["retry"]) if args.get("retry") else None)  # type: ignore[arg-type]
        env = {"BECK_CAUSE": command.id, "BECK_ITERATION": self.ctx.iteration, "BECK_ACTOR": agent,
               "BECK_PROJECT": str(self.ctx.project)}
        cwd = self.ctx.workspace.worktree(args["unit"]) if args.get("unit") and agent != "kent" else self.ctx.project
        try:
            turn = await self.agents.turn(agent, args["session"], envelope, env, cwd)
        except TurnFailed as error:
            text = str(error)[:2000] or "the turn failed"
            return [Result("AgentTurnFailed", {"agent": agent, "command": name, "error": text,
                                               "signature": signature([text])}, actor=agent)]
        tools = [Result("ToolCalled", {"agent": agent, "tool": t["tool"], "input": t["input"]}, actor=agent)
                 for t in turn.tools]
        ended = Result("AgentTurnEnded", {"agent": agent, "model": turn.model, "tokens": turn.tokens,
                                          **({"session": turn.session} if turn.session else {})},
                       actor=agent, tokens=turn.tokens)
        checked = protocol.check_output(agent, name, turn.output)
        if checked.output is None:
            return [*tools, ended, Result("SignalRejected", {"agent": agent, "command": name, "errors": checked.errors,
                                                     "signature": signature(checked.errors)}, actor=agent)]
        if checked.output["outcome"] == "question":
            return [*tools, ended, Result("QuestionAsked", {"agent": agent, "command": name,
                                                    "question": checked.output["question"]}, actor=agent)]
        try:
            translated = self.machine.translators[name](command, checked.output, self.ctx)
        except Exception as error:
            logger.exception("translator %s crashed", name)
            raise _Crashed([*tools, ended, crash("translator", name, error)]) from error
        return [*tools, ended, *translated]

    def _record_crash(self, command: Event, results: list[Result]) -> None:
        store = self.ctx.store
        with store.transaction():
            for r in results:
                e = store.append(r.type, r.payload, stream=r.stream or command.stream, actor=r.actor, cause=command,
                                 tokens=r.tokens)
                if e.type == "BeckCrashed":
                    self.crashes.append(e)
        self._crashed.add(command.id)

    def answer(self, command_id: str, results: list[Result]) -> None:
        """The user's answer to a pending command of theirs (a question, an approval)."""
        command = self.ctx.store.get(command_id)
        if command.payload["target"] != "user" or command not in self.pending():
            raise ValueError(f"{command_id} is not a pending command of the user")
        self._finish(command, results)

    async def run_until_idle(self) -> None:
        """Reacts and executes until nothing is left but the user's answers."""
        while True:
            progressed = self.react()
            for command in self.pending():
                target = command.payload["target"]
                if command.id in self._crashed:
                    continue
                if target == "beck":
                    try:
                        results = self._execute(command)
                    except Exception as error:
                        logger.exception("handler %s crashed", command.payload["command"])
                        self._record_crash(command, [crash("handler", command.payload["command"], error)])
                    else:
                        self._finish(command, results)
                    progressed = True
                elif target != "user" and command.id not in self._running and len(self._running) < self.slots:
                    self._running[command.id] = asyncio.create_task(self._turn(command))
            if progressed:
                continue
            if not self._running:
                return
            done, _ = await asyncio.wait(self._running.values(), return_when=asyncio.FIRST_COMPLETED)
            for cid, task in list(self._running.items()):
                if task in done:
                    del self._running[cid]
                    command = self.ctx.store.get(cid)
                    try:
                        results = task.result()
                    except _Crashed as crashed:
                        self._record_crash(command, crashed.results)
                    except Exception as error:
                        logger.exception("turn %s crashed", command.payload["command"])
                        self._record_crash(command, [crash("turn", command.payload["command"], error)])
                    else:
                        self._finish(command, results)

