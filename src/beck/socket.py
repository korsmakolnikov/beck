"""The session's socket: how the processes around a running session talk to it (docs/glossary.md, "Socket").

The session running in a folder serves `.beck/beck.sock` (or, for a folder too deep for a socket's path, a
socket under $XDG_RUNTIME_DIR named after the folder); the folder names the session, so a client needs no
id. One JSON object per line, each way:

* an agent's command (`beck send test …`, `beck send check …`): `{"command": "test" | "check", "args": {…},
  "cause": $BECK_CAUSE, "actor": $BECK_ACTOR}`; the engine runs it once, inside beck, records the run and
  answers `{"ok": true, "summary": …, "exit_code": …, "event": …}`, or `{"ok": false, "error": …}`;
* the dashboard: `{"hello": "dashboard"}`; the handshake answers where the read models and the log are
  (`{"iteration", "db", "log"}`), then the session sends `{"signal": "read"}` whenever they changed.

A client in the session's own process (the tests' agents) reaches the engine directly, as the socket would:
the same command, the same answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket as sockets
from collections.abc import Callable
from pathlib import Path
from typing import Any

NAME = "beck.sock"
MAX_PATH = 100   # a unix socket's path holds 108 bytes on Linux, fewer elsewhere

# the engines of this process, by their project's folder: a client here calls them as the socket would
_LOCAL: dict[Path, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def path(project: Path) -> Path:
    """Where the session of the project's folder listens."""
    here = Path(project).resolve() / ".beck" / NAME
    if len(str(here)) <= MAX_PATH:
        return here
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "beck"
    return runtime / (hashlib.sha256(str(Path(project).resolve()).encode()).hexdigest()[:16] + ".sock")


class NoSession(RuntimeError):
    """No session runs in the folder."""


def send(project: Path, message: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
    """One request to the folder's session, its answer."""
    local = _LOCAL.get(Path(project).resolve())
    if local is not None:
        return local(message)
    where = path(project)
    if not where.exists():
        raise NoSession(f"no beck session runs in {project}")
    with sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect(str(where))
        except (ConnectionRefusedError, FileNotFoundError) as error:
            raise NoSession(f"no beck session runs in {project} (a stale socket: {where})") from error
        s.sendall((json.dumps(message) + "\n").encode())
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buffer += chunk
    return json.loads(buffer.decode() or "{}")


class Server:
    """The session's side: answers the agents' commands through `handle`, greets the dashboards and signals
    them (`signal`)."""

    def __init__(self, project: Path, handle: Callable[[dict[str, Any]], dict[str, Any]], hello: dict[str, Any]):
        self.project, self.handle, self.hello = Path(project).resolve(), handle, hello
        self.where = path(self.project)
        self._dashboards: list[asyncio.StreamWriter] = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        _LOCAL[self.project] = self.handle
        self.where.parent.mkdir(parents=True, exist_ok=True)
        if self.where.exists():   # a session that died left it: nobody answers on it
            try:
                send_probe(self.where)
                raise RuntimeError(f"a beck session already runs in {self.project}")
            except NoSession:
                self.where.unlink()
        self._server = await asyncio.start_unix_server(self._client, path=str(self.where))

    async def stop(self) -> None:
        _LOCAL.pop(self.project, None)
        for w in self._dashboards:
            w.close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError):
            self.where.unlink()

    def signal(self) -> None:
        """The read models changed: every dashboard reads them again."""
        for w in list(self._dashboards):
            if w.is_closing():   # a dashboard that left
                self._dashboards.remove(w)
                continue
            try:
                w.write(b'{"signal": "read"}\n')
            except Exception:
                self._dashboards.remove(w)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                message = json.loads(line)
                if message.get("hello") == "dashboard":
                    writer.write((json.dumps(self.hello) + "\n").encode())
                    await writer.drain()
                    self._dashboards.append(writer)
                    continue
                try:
                    answer = self.handle(message)
                except Exception as error:   # the client learns it; the session goes on
                    answer = {"ok": False, "error": f"{type(error).__name__}: {error}"}
                writer.write((json.dumps(answer) + "\n").encode())
                await writer.drain()
        except (ConnectionResetError, json.JSONDecodeError):
            pass
        finally:   # the client left: a dashboard is signalled no more
            if writer in self._dashboards:
                self._dashboards.remove(writer)
            writer.close()


def send_probe(where: Path) -> None:
    """Whether a session answers on the socket; NoSession when nobody does."""
    with sockets.socket(sockets.AF_UNIX, sockets.SOCK_STREAM) as s:
        try:
            s.connect(str(where))
        except (ConnectionRefusedError, FileNotFoundError) as error:
            raise NoSession(str(where)) from error
