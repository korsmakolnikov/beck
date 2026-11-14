"""The Claude Code driver: one `claude -p` per turn, read as a stream.

* Every beck session (an agent and a session key, in an iteration) is one
  Claude Code session, with a stable uuid: the first turn starts it
  (`--session-id`), the next ones resume it (`--resume`), so unclebob keeps
  its group's context and martin its unit's.
* The agent's definition is built from its file (`beck/agents/prompts/`), the
  common protocol and the examples of its commands' outputs
  (`beck/agents/examples.yaml`): the prompt cannot drift from the schemas.
* The user message is the input envelope, as JSON; the answer is the result
  of the turn, parsed as JSON (a code fence around it is tolerated).
* The tools are the agent's own, `Bash` limited to beck's commands;
  anything else is denied without asking (`--permission-prompts none`).
* Every tool call of the stream is returned, for smart_audit; the tokens
  come from the result.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from beck.events.store import EventStore
from beck.machine.core import Turn, TurnFailed

PROMPTS = files("beck.agents") / "prompts"
EXAMPLES = files("beck.agents") / "examples.yaml"
NAMESPACE = uuid.UUID("6f1b1c2e-6f2a-4e8e-9b7d-6c0b6b2f1a10")
INPUT_CHARS = 2000   # a tool call's input, as recorded


def _front_matter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    _, head, body = text.split("---", 2)
    return yaml.safe_load(head) or {}, body.strip()


def definition(agent: str, model: str | None = None) -> dict[str, Any]:
    """The agent's definition for `--agents`: its prompt and its tools."""
    meta, body = _front_matter((PROMPTS / f"{agent}.md").read_text())
    # argyris talks with the user in a session of its own: beck's turn protocol is not its
    common = "" if agent == "argyris" else (PROMPTS / "common.md").read_text()
    examples = yaml.safe_load(EXAMPLES.read_text())[agent]
    shown = "\n\n".join(
        f"### {command}\n\nInput context:\n```json\n{json.dumps(e['in'], indent=1)}\n```\n\nOutput:\n```json\n"
        f"{json.dumps({'protocol': 1, 'command': command, **e['out']}, indent=1)}\n```"
        for command, e in examples.items())
    prompt = f"{body}\n\n{common}\n\n## Examples of your commands\n\n{shown}\n"
    tools = sorted({t.split("(")[0] for t in meta.get("tools", [])})
    out: dict[str, Any] = {"description": meta.get("description", agent), "prompt": prompt, "tools": tools}
    if model:
        out["model"] = model
    return out


def allowed_tools(agent: str) -> list[str]:
    meta, _ = _front_matter((PROMPTS / f"{agent}.md").read_text())
    return [str(t) for t in meta.get("tools", [])]


def session_id(iteration: str, agent: str, session: str) -> str:
    return str(uuid.uuid5(NAMESPACE, f"{iteration}/{agent}/{session}"))


_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def parse_answer(text: str) -> Any:
    """The JSON document of the answer; the text itself when there is none (the protocol refuses it)."""
    text = text.strip()
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return text
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return text


@dataclass
class ClaudeCode:
    store: EventStore
    project: Path
    iteration: str
    models: dict[str, str] = field(default_factory=dict)
    executable: str = "claude"
    timeout: float = 3600
    docs: Path | None = None              # beck's documentation, added to the sessions (the DSLs' manifest)

    def _agents_file(self) -> Path:
        path = self.project / ".beck" / "agents.json"
        agents = {a: definition(a, self.models.get(a)) for a in ("kent", "unclebob", "martin", "reader")}
        text = json.dumps(agents, indent=1)
        if not path.is_file() or path.read_text() != text:
            path.write_text(text)
        return path

    def _started(self, sid: str) -> bool:
        return any(e.payload.get("session") == sid for e in self.store.read(types_=("AgentTurnEnded",)))

    def argv(self, agent: str, sid: str) -> list[str]:
        argv = [self.executable, "-p", "--output-format", "stream-json", "--verbose",
                "--agents", str(self._agents_file()), "--agent", agent,
                "--permission-prompts", "none", "--allowedTools", *allowed_tools(agent)]
        if self.models.get(agent):
            argv += ["--model", self.models[agent]]
        if self.docs is not None:
            argv += ["--add-dir", str(self.docs)]
        argv += ["--resume", sid] if self._started(sid) else ["--session-id", sid]
        return argv

    async def turn(self, agent: str, session: str, envelope: dict[str, Any], env: dict[str, str],
                   cwd: Path) -> Turn:
        sid = session_id(self.iteration, agent, session)
        proc = await asyncio.create_subprocess_exec(
            *self.argv(agent, sid), cwd=cwd, env={**os.environ, **env},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(json.dumps(envelope).encode()), self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise TurnFailed(f"no answer in {self.timeout:.0f} s") from None
        return self.read(out.decode(errors="replace"), err.decode(errors="replace"), proc.returncode, agent, sid)

    def read(self, stdout: str, stderr: str, code: int | None, agent: str, sid: str) -> Turn:
        tools: list[dict[str, Any]] = []
        result: dict[str, Any] | None = None
        for line in stdout.splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("type") == "assistant":
                for block in message.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        text = json.dumps(block.get("input", {}))
                        tool_input = block.get("input", {}) if len(text) <= INPUT_CHARS else {"truncated": text[:INPUT_CHARS]}
                        tools.append({"tool": block.get("name", "?"), "input": tool_input})
            elif message.get("type") == "result":
                result = message
        if result is None or result.get("is_error"):
            reason = (result or {}).get("result") or stderr.strip()[-1500:] or f"exit {code}, no result"
            raise TurnFailed(str(reason))
        usage = result.get("usage", {})
        tokens = {"input": int(usage.get("input_tokens", 0)), "output": int(usage.get("output_tokens", 0)),
                  "cache_read": int(usage.get("cache_read_input_tokens", 0)),
                  "cache_creation": int(usage.get("cache_creation_input_tokens", 0))}
        model = self.models.get(agent) or next(iter(result.get("modelUsage", {}) or {}), "default")
        return Turn(parse_answer(str(result.get("result", ""))), model, tokens, sid, tools)
