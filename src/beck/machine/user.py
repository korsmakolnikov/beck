"""The user's decisions: how a pending command of the user becomes events from the user's words.

Every command addressed to the user is one of these. An approval reads
"yes" (or "ok", "approve", "y") as approved; any other text is the
instructions of a revision. A choice must be one of its options. The words
are kept as they are: a translation layer (deterministic, a small model as
fallback) may come later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from beck.events.store import Event
from beck.machine.core import Result

BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
YES = {"yes", "y", "ok", "approve", "approved", "accept", "sì", "si"}


@dataclass(frozen=True)
class Decision:
    kind: str                         # approval | choice | text
    approved: str | None = None       # the event of a yes
    revised: str | None = None        # the event of another text, with {"instructions": text}
    event: str | None = None          # the event of a choice or a text
    field: str = "text"               # the payload's field the words go to
    options: tuple[str, ...] = ()


DECISIONS: dict[str, Decision] = {
    "Ask": Decision("text", event="QuestionAnswered"),
    "AskInterview": Decision("text", event="InterviewAnswered", field="answer"),
    "ClassifyProject": Decision("choice", event="ProjectClassified", field="kind",
                                options=("greenfield", "brownfield")),
    "ConfirmBranchName": Decision("text", event="BranchNamed", field="name"),
    "ConfirmToolchain": Decision("choice", event="ToolchainConfirmed", field="language", options=("python", "elixir")),
    "ApproveShoppingList": Decision("approval", "ShoppingListApproved", "ShoppingListRevised"),
    "ApproveDslSync": Decision("approval", "DslSyncApproved", "DslSyncRejected"),
    "ApproveDomain": Decision("approval", "DomainApproved", "DomainRevised"),
    "ApproveArchitecture": Decision("approval", "ArchitectureApproved", "ArchitectureRevised"),
    "ApproveRestart": Decision("approval", "RestartApproved", "RestartRejected"),
    "ApproveTradeoff": Decision("approval", "TradeoffAccepted", "TradeoffRejected"),
    "PresentForReview": Decision("approval", "ReviewAccepted", "CorrectionsRequested"),
    "ApproveSuiteRuns": Decision("approval", "SuiteRunsApproved", "SuiteRunsRefused"),
}


class DecisionError(ValueError):
    pass


def answer(command: Event, words: str) -> list[Result]:
    """The events of the user's answer to a pending command of theirs."""
    name = command.payload["command"]
    decision = DECISIONS.get(name)
    if decision is None:
        raise DecisionError(f"{name} is not a decision of the user")
    words = words.strip()
    if not words:
        raise DecisionError("an empty answer")
    args = command.payload["args"]
    if decision.kind == "approval":
        if words.lower() in YES:
            return [Result(decision.approved or "", {}, actor="user")]
        field = "text" if decision.revised == "CorrectionsRequested" else "instructions"
        return [Result(decision.revised or "", {field: words}, actor="user")]
    if decision.kind == "choice":
        if words.lower() not in decision.options:
            raise DecisionError(f"one of: {', '.join(decision.options)}")
        return [Result(decision.event or "", {decision.field: words.lower()}, actor="user")]
    payload: dict[str, Any] = {decision.field: words}
    if name == "AskInterview":
        payload = {"key": args["key"], "question": args["question"], "answer": words}
    if name == "ConfirmBranchName":
        payload = {"name": args["proposed"] if words.lower() in YES else words}
        if not BRANCH.match(payload["name"]) or ".." in payload["name"] or payload["name"].endswith((".", "/")):
            raise DecisionError(f"{payload['name']!r} is not a branch name: yes, or a name without spaces")
    return [Result(decision.event or "", payload, actor="user")]


def prompt(command: Event) -> str:
    """What the user reads for a pending command."""
    name, args = command.payload["command"], command.payload["args"]
    if name in ("Ask",):
        return args["question"]["text"]
    if name == "AskInterview":
        return args["question"]
    if name == "ClassifyProject":
        return "The project holds documents only: is it greenfield or brownfield?"
    if name == "ConfirmBranchName":
        return f"Branch name: {args['proposed']} (yes, or another name)"
    if name == "ConfirmToolchain":
        return f"The stack looks like {args['detected']}: python or elixir?"
    if name == "PresentForReview":
        # the notes first: an unimplemented left matters more than the commits, and a long list would push them out of sight
        notes = ("To look at:\n" + "\n".join(f"- {n}" for n in args["notes"]) + "\n") if args.get("notes") else ""
        return "Review the branch (yes to close the iteration, or the corrections):\n" + notes + "\n".join(args["commits"])
    if name == "ApproveRestart":
        p = args["proposal"]
        return f"Restart: {p['summary']}\n{p['changes']}\n(yes, or what to change)"
    if name == "ApproveSuiteRuns":
        return (f"{args['test']} looks flaky ({args['evidence']}), but alone and in its component it was steady: "
                f"run the whole suite {args['runs']} times to find out? (yes, or no)")
    if name == "ApproveTradeoff":
        return f"A refactor of {args['component']} worsens {', '.join(args['worsened'])}: accept? (yes, or why not)"
    return f"{name}: {args.get('file') or args.get('summary') or ''} (yes, or what to change)"
