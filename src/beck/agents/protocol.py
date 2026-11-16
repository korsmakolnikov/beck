"""The protocol of the agents' turns: JSON in, JSON out, both validated (docs/protocol.md).

An input that fails its schema is beck's bug and raises; an output that
fails is the agent's, and becomes a SignalRejected with the errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any

import jsonschema
import yaml
from referencing import Registry, Resource

SCHEMAS = files("beck.agents") / "schemas"
AGENTS = ("kent", "unclebob", "martin", "reader")
BASE = "https://beck.local/agents/"


@cache
def _registry() -> Registry:
    resources = []
    for name in ("common", *AGENTS):
        schema = yaml.safe_load((SCHEMAS / f"{name}.schema.yaml").read_text())
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


@cache
def _validator(ref: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator({"$ref": BASE + ref}, registry=_registry())


@cache
def commands(agent: str) -> tuple[str, ...]:
    schema = _registry().contents(f"{BASE}{agent}.schema.yaml")
    return tuple(sorted(k[:-3] for k in schema["$defs"] if k.endswith("-in")))


def _errors(ref: str, doc: Any) -> list[str]:
    errors = sorted(_validator(ref).iter_errors(doc), key=lambda e: list(map(str, e.path)))
    return [f"{'.'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in errors]


class ProtocolError(ValueError):
    pass


def check_input(doc: Any) -> None:
    """Raises on an input beck must never send."""
    errors = _errors("common.schema.yaml#/$defs/input", doc)
    if not errors:
        agent, command = doc["agent"], doc["command"]
        if command not in commands(agent):
            errors = [f"command: {agent} has no command {command!r}"]
        elif "answer" in doc:
            errors = [] if doc["context"] == {} else ["context: an answer goes on with the same context: send {}"]
        else:
            errors = [f"context.{e}" for e in _errors(f"{agent}.schema.yaml#/$defs/{command}-in", doc["context"])]
    if errors:
        raise ProtocolError("; ".join(errors))


def _forbids_result(agent: str, command: str) -> bool:
    return bool(_errors(f"{agent}.schema.yaml#/$defs/{command}-out",
                        {"protocol": 1, "command": command, "outcome": "done", "result": {}}))


@dataclass(frozen=True)
class Checked:
    output: dict[str, Any] | None   # None when rejected
    errors: list[str]


def check_output(agent: str, command: str, doc: Any) -> Checked:
    """The agent's answer, or the errors that make it a SignalRejected."""
    if isinstance(doc, dict) and isinstance(doc.get("result"), dict) and _forbids_result(agent, command) \
            and _errors(f"{agent}.schema.yaml#/$defs/{command}-out", {k: v for k, v in doc.items() if k != "result"}) == []:
        # a result where none is due ({} or a summary of what was done) tells the machine nothing: beck verifies
        doc = {k: v for k, v in doc.items() if k != "result"}
    errors = _errors("common.schema.yaml#/$defs/output", doc)
    if not errors and doc["command"] != command:
        errors = [f"command: the answer is for {doc['command']!r}, not {command!r}"]
    if not errors:
        errors = _errors(f"{agent}.schema.yaml#/$defs/{command}-out", doc)
    if not errors and agent != "kent" and doc["outcome"] == "question":
        errors = ["outcome: only kent asks the user"]
    if not errors:
        errors = brevity(doc)
    return Checked(None if errors else doc, errors)


BRIEF = ("summary", "problem", "why")     # what it is, in a sentence, two at most
SUBJECT = 72                              # a commit's subject: one sentence, the length git shows
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=\S)")


def sentences(text: str) -> int:
    return len([s for s in _SENTENCE.split(text.strip()) if s])


def brevity(doc: Any, path: str = "") -> list[str]:
    """The agents' free text kept brief (the user's rule): a subject is one sentence of at most 72 characters;
    a summary, a problem, a why at most two sentences. A long answer is a SignalRejected, retried."""
    errors: list[str] = []
    if isinstance(doc, dict):
        for key, value in doc.items():
            where = f"{path}.{key}" if path else key
            if isinstance(value, str) and key == "subject":
                if len(value) > SUBJECT:
                    errors.append(f"{where}: at most {SUBJECT} characters")
                if sentences(value) > 1:
                    errors.append(f"{where}: one sentence")
            elif isinstance(value, str) and key in BRIEF and sentences(value) > 2:
                errors.append(f"{where}: at most 2 sentences, it has {sentences(value)}")
            else:
                errors += brevity(value, where)
    elif isinstance(doc, list):
        for i, item in enumerate(doc):
            errors += brevity(item, f"{path}[{i}]")
    return errors


def envelope(agent: str, command: str, cause_id: str, correlation_id: str, context: dict[str, Any],
             retry: tuple[int, str] | None = None) -> dict[str, Any]:
    """A checked input for one turn."""
    doc: dict[str, Any] = {"protocol": 1, "agent": agent, "command": command,
                           "cause": {"event_id": cause_id, "correlation_id": correlation_id}, "context": context}
    if retry is not None:
        doc["retry"] = {"attempt": retry[0], "reason": retry[1]}
    check_input(doc)
    return doc


def answer(agent: str, command: str, cause_id: str, correlation_id: str, question: dict[str, Any],
           text: str) -> dict[str, Any]:
    """The user's answer, wrapped for the next turn of the session that asked."""
    doc: dict[str, Any] = {"protocol": 1, "agent": agent, "command": command,
                           "cause": {"event_id": cause_id, "correlation_id": correlation_id}, "context": {},
                           "answer": {"question": question, "text": text}}
    check_input(doc)
    return doc
