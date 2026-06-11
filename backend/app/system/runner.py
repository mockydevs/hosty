"""Single entrypoint for executing host commands.

Shell injection is impossible by construction: commands are exec'd from argv
lists, never interpreted by a shell. This is the only module in the codebase
allowed to spawn processes (CI-enforced).
"""

from __future__ import annotations

import asyncio
import time
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
) -> CommandResult:
    cmd = validate_argv(argv)
    start = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except FileNotFoundError as exc:
        raise CommandNotFoundError(f"Executable not found: {cmd[0]}") from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        proc.kill()
        await proc.wait()
        raise CommandTimeoutError(f"Command timed out after {timeout}s: {cmd[0]}") from exc

    duration_ms = round((time.perf_counter() - start) * 1000, 1)
    result = CommandResult(
        argv=cmd,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout_b.decode(errors="replace"),
        stderr=stderr_b.decode(errors="replace"),
        duration_ms=duration_ms,
    )
    log.info(
        "command_executed",
        argv=list(cmd),
        returncode=result.returncode,
        duration_ms=duration_ms,
    )
    return result
