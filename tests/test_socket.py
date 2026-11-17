"""The session's socket: the agents' commands and the dashboards' signals, over a real unix socket."""

import asyncio

import pytest

from beck import socket


def test_a_command_goes_through_the_socket_and_its_answer_comes_back(tmp_path):
    seen = []

    async def play():
        server = socket.Server(tmp_path, lambda m: (seen.append(m), {"ok": True, "summary": "ALL_OK"})[1], {})
        await server.start()
        socket._LOCAL.pop(tmp_path.resolve())   # another process: through the socket itself
        try:
            return await asyncio.to_thread(socket.send, tmp_path, {"command": "test", "cause": "c"}, 5)
        finally:
            await server.stop()
    assert asyncio.run(play()) == {"ok": True, "summary": "ALL_OK"}
    assert seen == [{"command": "test", "cause": "c"}]
    assert not socket.path(tmp_path).exists()            # the session gone, its socket too


def test_a_socket_a_dead_session_left_is_taken_over(tmp_path):
    where = socket.path(tmp_path)
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text("")                                 # nobody listens on it
    with pytest.raises(socket.NoSession, match="stale"):
        socket.send(tmp_path, {"command": "test"})

    async def play():
        server = socket.Server(tmp_path, lambda m: {"ok": True}, {})
        await server.start()
        await server.stop()
    asyncio.run(play())


def test_a_folder_too_deep_for_a_socket_s_path_gets_one_under_the_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    deep = tmp_path / ("d" * 120)
    assert socket.path(deep).parent == tmp_path / "run" / "beck"
    assert socket.path(tmp_path / "p") == tmp_path / "p" / ".beck" / "beck.sock" or len(str(tmp_path)) > 80
