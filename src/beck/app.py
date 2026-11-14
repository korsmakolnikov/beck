"""beck as an application: start or attach a session, answer the user's decisions.

Interactive (a terminal): beck runs, asks each decision on the terminal and
goes on. Not interactive (stdin is not a terminal, or --batch): beck runs
until a decision of the user is pending, prints it with its id, and exits;
`beck answer` records the answer and `beck attach` goes on. Either way
Ctrl-C is safe: the session is resumed from its events.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TextIO

import beck
from beck import config as beck_config
from beck.drivers.claude import ClaudeCode
from beck.events.project import Project
from beck.events.store import Event, EventStore
from beck.machine import user
from beck.machine.core import Agents, Ctx, Reactor
from beck.machine.session import IndexWorkspace, beck_machine

DONE = ("IterationClosed", "ConfigInvalid")


def docs_dir() -> Path | None:
    docs = Path(beck.__file__).resolve().parents[2] / "docs"
    return docs if docs.is_dir() else None


def reactor_for(project: Project, iteration: str, store: EventStore, agents: Agents | None = None) -> Reactor:
    try:
        config = beck_config.load(project.root)
    except beck_config.ConfigError:
        config = dict(beck_config.DEFAULTS)  # LoadConfig reports it as an event
    ctx = Ctx(store, project.root, iteration, IndexWorkspace(store, project.root),
              limits=(config["limits"]["times"], config["limits"]["attempts"]),
              units_in_parallel=config["units_in_parallel"],
              flakiness=(config["flakiness"]["window"], config["flakiness"]["runs"]))
    driver = agents or ClaudeCode(store, project.root, iteration, config["models"],
                                  executable=os.environ.get("BECK_CLAUDE", "claude"), docs=docs_dir())
    return Reactor(ctx, beck_machine, driver, slots=config["units_in_parallel"])


def pending_decisions(reactor: Reactor) -> list[Event]:
    return [c for c in reactor.pending() if c.payload["target"] == "user"]


def finished(store: EventStore) -> Event | None:
    return next(iter(store.read(types_=DONE)), None)


def show(command: Event, out: TextIO) -> None:
    out.write(f"[{command.id[:8]}] {command.payload['command']}: {user.prompt(command)}\n")


async def drive(reactor: Reactor, interactive: bool, read: TextIO, out: TextIO) -> int:
    """Runs until the end, or (not interactive) until a decision waits; the exit code."""
    store = reactor.ctx.store
    while True:
        await reactor.run_until_idle()
        if reactor.crashes:
            for c in reactor.crashes:
                out.write(f"beck crashed in the {c.payload['where']} {c.payload['name']}: {c.payload['error']}\n")
            out.write(f"beck: fix it, then `beck attach {reactor.ctx.iteration[:8]}` resumes (`beck audit`)\n")
            return 1
        end = finished(store)
        if end is not None:
            out.write(f"beck: {end.type}" + (f": {end.payload.get('errors')}" if end.payload.get("errors") else "")
                      + "\n")
            return 0 if end.type == "IterationClosed" else 1
        waiting = pending_decisions(reactor)
        if not waiting:
            out.write("beck: nothing to do and nothing to answer: see `beck events`\n")
            return 1
        if not interactive:
            for command in waiting:
                show(command, out)
            out.write(f"beck: answer with `beck answer {reactor.ctx.iteration[:8]} <id> \"...\"`, "
                      f"then `beck attach {reactor.ctx.iteration[:8]}`\n")
            return 2
        command = waiting[0]
        show(command, out)
        while True:
            out.write("> ")
            out.flush()
            line = read.readline()
            if not line:
                return 2
            try:
                reactor.answer(command.id, user.answer(command, line))
                break
            except user.DecisionError as error:
                out.write(f"beck: {error}\n")


def run(project_root: Path, iteration: str | None, interactive: bool | None = None,
        agents: Agents | None = None, read: TextIO | None = None, out: TextIO | None = None) -> int:
    read, out = read or sys.stdin, out or sys.stdout
    project = Project(project_root)
    if iteration is None:
        it, store, _ = project.start_iteration()
        out.write(f"beck: iteration {it.id[:8]} started\n")
    else:
        it = project.find(iteration)
        store = project.open(it.id)
        last = store.last()
        store.append("SessionAttached", {"from_seq": last.seq if last else 0},
                     cause=next(iter(store.read(types_=("SessionStarted",))), None))
    reactor = reactor_for(project, it.id, store, agents)
    interactive = sys.stdin.isatty() if interactive is None else interactive
    try:
        return asyncio.run(drive(reactor, bool(interactive), read, out))
    except KeyboardInterrupt:
        store.append("SessionInterrupted", {"reason": "Ctrl-C"})
        out.write(f"\nbeck: interrupted; resume with `beck attach {it.id[:8]}`\n")
        return 130
    finally:
        store.close()


def pending(project_root: Path, iteration: str, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    project = Project(project_root)
    it = project.find(iteration)
    store = project.open(it.id)
    try:
        reactor = reactor_for(project, it.id, store)
        waiting = pending_decisions(reactor)
        for command in waiting:
            show(command, out)
        return 0 if waiting else 1
    finally:
        store.close()


def answer(project_root: Path, iteration: str, command_prefix: str, words: str, out: TextIO | None = None) -> int:
    out = out or sys.stdout
    project = Project(project_root)
    it = project.find(iteration)
    store = project.open(it.id)
    try:
        reactor = reactor_for(project, it.id, store)
        matches = [c for c in pending_decisions(reactor) if c.id.startswith(command_prefix)]
        if len(matches) != 1:
            out.write(f"beck: {len(matches)} pending decisions match {command_prefix!r}\n")
            return 1
        try:
            reactor.answer(matches[0].id, user.answer(matches[0], words))
        except user.DecisionError as error:
            out.write(f"beck: {error}\n")
            return 1
        out.write(f"beck: answered; go on with `beck attach {it.id[:8]}`\n")
        return 0
    finally:
        store.close()
