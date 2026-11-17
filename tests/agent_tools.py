"""What a test's agent runs from its turn: `beck send`, through the session's socket (in the same process, the
engine answers directly)."""

import os
from pathlib import Path

from beck import socket


def send_test(cwd, tests, component=None, requirement=None, env=None):
    env = env or os.environ
    answer = socket.send(Path(env["BECK_PROJECT"]), {
        "command": "test", "cause": env["BECK_CAUSE"], "actor": env.get("BECK_ACTOR", "unclebob"),
        "args": {"tests": list(tests), "component": component, "requirement": requirement, "cwd": str(cwd)}})
    assert answer.get("ok"), answer
    return answer
