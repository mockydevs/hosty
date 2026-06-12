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

# The panel service environment contains signing keys and infrastructure
# credentials. Child processes receive only ordinary process-locale/runtime
# variables plus explicit caller overrides.
SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        # Windows development/test process discovery.
        "SYSTEMROOT",
        "WINDIR",
        "PATHEXT",
        "COMSPEC",
    }
)


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


def build_subprocess_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    child_env = {key: value for key, value in os.environ.items() if key.upper() in SAFE_ENV_KEYS}
    if overrides:
        for key, value in overrides.items():
            if not isinstance(key, str) or not isinstance(value, str) or "\x00" in key + value:
                raise InvalidCommandError("environment overrides must be NUL-free strings")
            child_env[key] = value
    return child_env


async def run(
    argv: list[str] | tuple[str, ...],
    *,
    timeout: float = 30.0,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    stdout_path: str | None = None,
    stdin_path: str | None = None,
    umask: int = 0o027,
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
            process_kwargs = {
                "stdin": stdin_f,
                "stdout": stdout_f if stdout_f is not None else asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "cwd": cwd,
                "env": build_subprocess_env(env),
            }
            if os.name != "nt":
                process_kwargs["umask"] = umask
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                **process_kwargs,
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
        executable=cmd[0],
        argument_count=len(cmd) - 1,
        returncode=result.returncode,
        duration_ms=duration_ms,
    )
    return result
