from __future__ import annotations

import pytest

from app.system import runner


async def test_run_captures_stdout():
    result = await runner.run(["echo", "hello"])
    assert result.ok
    assert result.stdout.strip() == "hello"
    assert result.returncode == 0
    assert result.duration_ms >= 0


async def test_run_nonzero_exit():
    result = await runner.run(["false"])
    assert not result.ok
    assert result.returncode == 1


async def test_run_timeout_kills_process():
    with pytest.raises(runner.CommandTimeoutError):
        await runner.run(["sleep", "5"], timeout=0.2)


async def test_run_command_not_found():
    with pytest.raises(runner.CommandNotFoundError):
        await runner.run(["definitely-not-a-real-binary-xyz"])


async def test_shell_metacharacters_are_not_interpreted():
    """The heart of the no-injection guarantee: metachars are literal arguments."""
    payload = "$(whoami); rm -rf / && echo pwned | cat"
    result = await runner.run(["echo", payload])
    assert result.stdout.strip() == payload


def test_validate_rejects_empty_argv():
    with pytest.raises(runner.InvalidCommandError):
        runner.validate_argv([])


def test_validate_rejects_non_string_items():
    with pytest.raises(runner.InvalidCommandError):
        runner.validate_argv(["echo", 42])  # type: ignore[list-item]


def test_validate_rejects_nul_bytes():
    with pytest.raises(runner.InvalidCommandError):
        runner.validate_argv(["echo", "a\x00b"])


def test_validate_rejects_blank_program():
    with pytest.raises(runner.InvalidCommandError):
        runner.validate_argv(["", "arg"])
