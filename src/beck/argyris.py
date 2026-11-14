"""The outbox's reader: smart_audit's violations, as a structured message for argyris (README, "Actors").

`message` gathers the records not handled yet into one ReviewViolations
input, each with the files that hold the rule it breaks; `wake` opens
argyris's session, one of beck's agents, in beck's source (interactive:
argyris settles every violation with the user and proposes each change as a
pull request, `beck contrib`) with that message; `settle` takes argyris's JSON answer, checks it
against its schema, marks the records handled with how they were settled,
and records a ViolationSettled in the iteration each came from, caused by
the event the violation is about, so the settlement keeps its correlation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import beck
from beck import config as beck_config
from beck import contrib
from beck.agents import protocol
from beck.drivers.claude import allowed_tools, definition
from beck.events.project import OutboxRecord, Project

SCHEMA_KINDS = ("malformed-output",)


def package_prefix() -> str:
    """Where beck's package sits in its source: `src/beck` in its repository, another path in a repository that holds it."""
    here = Path(beck.__file__).resolve().parent
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=here, capture_output=True, text=True)
    if top.returncode != 0:
        return "src/beck"
    return str(here.relative_to(Path(top.stdout.strip()).resolve()))


def _rules(record: OutboxRecord) -> list[str]:
    agent, prefix = record.payload.get("agent", "?"), package_prefix()
    rules = [f"{prefix}/agents/prompts/{agent}.md", f"{prefix}/agents/prompts/common.md"]
    if record.kind in SCHEMA_KINDS:
        rules.append(f"{prefix}/agents/schemas/{agent}.schema.yaml")
    return rules


def message(project: Project) -> dict[str, Any] | None:
    """The ReviewViolations input of the records not handled yet; None when there is none."""
    records = project.pending()
    if not records:
        return None
    violations = [{"id": r.id, "iteration": r.iteration, "event_id": r.event_id, "correlation_id": r.correlation_id,
                   "kind": r.kind, "agent": r.payload.get("agent", "?"),
                   "detail": {k: v for k, v in r.payload.items() if k != "agent"}, "rules": _rules(r)}
               for r in records]
    first = records[0]
    return protocol.envelope("argyris", "ReviewViolations", first.event_id, first.correlation_id,
                             {"project": str(project.root.resolve()), "violations": violations})


def prompt(doc: dict[str, Any]) -> str:
    project = doc["context"]["project"]
    return (f"beck's outbox holds {len(doc['context']['violations'])} violations found by smart_audit "
            f"in the project {project}. Settle them with the user, propose each change as a pull request, "
            f"then send your answer, one JSON document, to `beck audit --settle --project {project}` on its "
            f"standard input.\n\n```json\n{json.dumps(doc, indent=1)}\n```\n")


def wake(project: Project, executable: str | None = None) -> int:
    """Opens argyris's session, in beck's source, with the message; 1 when there is nothing to send."""
    doc = message(project)
    if doc is None:
        return 1
    config = beck_config.load(project.root)
    agents = project.beck_dir / "argyris.json"
    agents.write_text(json.dumps({"argyris": definition("argyris", config["models"].get("argyris"))}, indent=1))
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}"}
    argv = [executable or os.environ.get("BECK_CLAUDE", "claude"), prompt(doc), "--agents", str(agents),
            "--agent", "argyris", "--allowedTools", *allowed_tools("argyris")]
    return subprocess.run(argv, check=False, cwd=contrib.source(config), env=env).returncode


def settle(project: Project, doc: Any) -> list[str]:
    """Marks the settled records handled; the errors, when the answer is not valid."""
    checked = protocol.check_output("argyris", "ReviewViolations", doc)
    if checked.output is None:
        return checked.errors
    pending = {r.id: r for r in project.pending()}
    settled = checked.output["result"]["settled"]
    unknown = [s["id"] for s in settled if s["id"] not in pending]
    if unknown:
        return [f"settled.id: not pending in the outbox: {', '.join(unknown)}"]
    for s in settled:
        how = {k: v for k, v in s.items() if k != "id"}
        project.handle(s["id"], "argyris", how)
        record = pending[s["id"]]
        store = project.open(record.iteration)
        try:
            store.append("ViolationSettled", {"outbox": s["id"], **how}, actor="argyris",
                         cause=store.get(record.event_id), stream="audit")
        finally:
            store.close()
    return []
