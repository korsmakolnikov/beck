import io
import subprocess
import sys
from pathlib import Path

import pytest

from hello.greeting.greeter import main, the_greeting


def test_greeting_writes_nothing_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    # spec: greeting-writes-nothing-on-stderr
    main()
    assert capsys.readouterr().err == ""


def test_greeting_exits_with_status_zero() -> None:
    # spec: greeting-exits-with-status-zero
    assert main(io.StringIO()) == 0


@pytest.mark.parametrize(
    "command",
    [
        [str(Path(sys.executable).parent / "hello")],
        [sys.executable, "-m", "hello"],
    ],
    ids=["console-script", "python-m"],
)
def test_program_run_as_a_process_greets_and_exits_with_status_zero(command: list[str]) -> None:
    # spec: greeting-exits-with-status-zero
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert (result.stdout, result.stderr, result.returncode) == ("Hello, world!\n", "", 0)


def test_greeting_is_written_on_stdout_followed_by_one_newline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # spec: greeting-is-written-on-stdout
    main()
    assert capsys.readouterr().out == "Hello, world!\n"


def test_greeting_text_is_hello_world() -> None:
    # spec: greeting-text-is-hello-world
    assert the_greeting().text == "Hello, world!"
