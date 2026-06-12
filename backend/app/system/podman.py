"""Rootless Podman seam (v2/M2, ADR-013). The ONLY module that talks to
podman. Every call goes through the TENANT's user socket (`--url unix:
/run/user/<uid>/podman/podman.sock`) — the panel never runs podman as the
tenant via su, and never touches root podman storage.

Pure argv builders + TOTAL parsers (never raise on garbage — the observer
must keep working when a container returns unexpected JSON), with thin
async wrappers over runner.run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.core.errors import AppError
from app.domain.validate import validate_image_ref, validate_object_name
from app.system import runner

MANAGED_LABEL = "hosty.managed=1"
STACK_LABEL = "hosty.stack"


class PodmanError(AppError):
    status_code = 502
    code = "podman_error"


class PodmanUnavailableError(PodmanError):
    code = "podman_unavailable"


class InvalidPodmanArgError(runner.InvalidCommandError):
    pass


@dataclass(frozen=True)
class PsContainer:
    """One row of `podman ps --format json`, reduced to what observers need."""

    name: str
    image: str
    state: str  # created | running | paused | exited | ... | unknown
    running: bool
    stack: str | None  # hosty.stack label, None for unlabelled containers


def _validate_uid(uid: int) -> int:
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise InvalidPodmanArgError(f"Invalid uid: {uid!r}")
    return uid


def socket_url(uid: int) -> str:
    return f"unix:/run/user/{_validate_uid(uid)}/podman/podman.sock"


def _base(uid: int) -> list[str]:
    return ["podman", "--url", socket_url(uid)]


# --- argv builders (pure) ----------------------------------------------------------


def build_ps_argv(uid: int) -> list[str]:
    """All panel-managed containers of one tenant, machine-readable."""
    return [
        *_base(uid),
        "ps",
        "--all",
        "--filter",
        f"label={MANAGED_LABEL}",
        "--format",
        "json",
    ]


def build_pull_argv(uid: int, image: str) -> list[str]:
    return [*_base(uid), "image", "pull", "--quiet", validate_image_ref(image)]


def build_image_digest_argv(uid: int, image: str) -> list[str]:
    return [
        *_base(uid),
        "image",
        "inspect",
        "--format",
        "{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}",
        validate_image_ref(image),
    ]


def build_exec_argv(uid: int, container: str, command: list[str]) -> list[str]:
    """Run a blueprint action inside a stack container (M5: WP-CLI etc.).
    `command` comes from blueprint CODE, never from users — but it still
    must be NUL-free strings, enforced by runner.validate_argv."""
    if not command:
        raise InvalidPodmanArgError("exec command must be non-empty")
    return [*_base(uid), "exec", validate_object_name(container), *command]


# --- parsers (total — never raise) -------------------------------------------------


def parse_ps(stdout: str) -> list[PsContainer]:
    try:
        raw = json.loads(stdout.strip() or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    containers: list[PsContainer] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        names = entry.get("Names")
        name = names[0] if isinstance(names, list) and names and isinstance(names[0], str) else ""
        if not name:
            continue
        labels = entry.get("Labels")
        labels = labels if isinstance(labels, dict) else {}
        state = str(entry.get("State") or "unknown")
        containers.append(
            PsContainer(
                name=name,
                image=str(entry.get("Image") or ""),
                state=state,
                running=state == "running",
                stack=labels.get(STACK_LABEL),
            )
        )
    return containers


def parse_digest(stdout: str) -> str | None:
    digest = stdout.strip()
    return digest if digest and "@sha256:" in digest else None


# --- async operations --------------------------------------------------------------


async def _run_or_raise(argv: list[str], what: str, *, timeout: float = 60) -> runner.CommandResult:
    try:
        result = await runner.run(argv, timeout=timeout)
    except runner.CommandNotFoundError as exc:
        raise PodmanUnavailableError("Podman is not installed on this host") from exc
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise PodmanError(f"{what} failed: {detail[:400]}")
    return result


async def ps(uid: int) -> list[PsContainer]:
    """Graceful observe: an unreachable tenant socket reports [] rather than
    failing the whole reconcile cycle (the planner then repairs by restart)."""
    try:
        result = await runner.run(build_ps_argv(uid), timeout=30)
    except runner.CommandNotFoundError:
        return []
    if not result.ok:
        return []
    return parse_ps(result.stdout)


async def pull_image(uid: int, image: str) -> None:
    await _run_or_raise(build_pull_argv(uid, image), "Image pull", timeout=600)


async def resolve_digest(uid: int, image: str) -> str | None:
    """RepoDigest of an image present in the tenant's storage."""
    result = await _run_or_raise(build_image_digest_argv(uid, image), "Image inspect", timeout=30)
    return parse_digest(result.stdout)


async def exec_in(
    uid: int, container: str, command: list[str], *, timeout: float = 120
) -> runner.CommandResult:
    """Blueprint action escape hatch — the result (incl. non-zero exit) is
    the blueprint's to interpret, so no _run_or_raise here."""
    try:
        return await runner.run(build_exec_argv(uid, container, command), timeout=timeout)
    except runner.CommandNotFoundError as exc:
        raise PodmanUnavailableError("Podman is not installed on this host") from exc
