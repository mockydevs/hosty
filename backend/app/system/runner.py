"""Single entrypoint for executing host commands.

Shell injection is impossible by construction: commands are exec'd from argv
lists, never interpreted by a shell. This is the only module in the codebase
allowed to spawn processes (CI-enforced).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass

import structlog

log = structlog.get_logger("hosty.system")


class InvalidCommandError(ValueError):
    """An argv list failed validation before execution."""


class CommandNotFoundError(RuntimeError):
    """The executable does not exist on this host."""


class CommandTimeoutError(RuntimeError):
    """The command exceeded its timeout and was killed."""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def validate_argv(argv: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(argv, (list, tuple)) or len(argv) == 0:
        raise InvalidCommandError("argv must be a non-empty list of strings")
    for item in argv:
        if not isinstance(item, str):
            raise InvalidCommandError(f"argv items must be strings, got {type(item).__name__}")
        if "\x00" in item:
            raise InvalidCommandError("argv items must not contain NUL bytes")
    if not argv[0].strip():
        raise InvalidCommandError("argv[0] (the program) must be non-empty")
    return tuple(argv)


async def run(
    argv: list[str] | tuple[str, ...],
    *,
    timeout: float = 30.0,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    stdout_path: str | None = None,
    stdin_path: str | None = None,
) -> CommandResult:
    """Execute argv. `stdout_path`/`stdin_path` redirect to/from files so that
    large streams (database dumps/restores) never pass through memory or a
    shell pipe. With `stdout_path` set, `result.stdout` is empty."""
    cmd = validate_argv(argv)
    start = time.perf_counter()

    stdout_f = open(stdout_path, "wb") if stdout_path else None  # noqa: SIM115
    stdin_f = open(stdin_path, "rb") if stdin_path else None  # noqa: SIM115
    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=stdin_f,
                stdout=stdout_f if stdout_f is not None else asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env={**os.environ, **env} if env is not None else None,
            )
        except FileNotFoundError as exc:
            raise CommandNotFoundError(f"Executable not found: {cmd[0]}") from exc

        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            proc.kill()
            await proc.wait()
            raise CommandTimeoutError(f"Command timed out after {timeout}s: {cmd[0]}") from exc
    finally:
        if stdout_f is not None:
            stdout_f.close()
        if stdin_f is not None:
            stdin_f.close()

    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    result = CommandResult(
        argv=cmd,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_b.decode(errors="replace") if stdout_b is not None else "",
        stderr=stderr_b.decode(errors="replace") if stderr_b is not None else "",
        duration_ms=duration_ms,
    )
    log.info(
        "command_executed",
        argv=list(cmd),
        returncode=result.returncode,
        duration_ms=duration_ms,
    )
    return result
