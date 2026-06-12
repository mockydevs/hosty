"""Docker engine seam (ADR-011). The ONLY module allowed to talk to Docker.

Pure argv builders (unit-testable, injection-impossible: no shell, and every
user-influenced value is validated against an allowlist grammar that rejects
leading dashes — argv option injection) executed through `runner.run`.

12a invariants live HERE, not in callers: loopback-only port publishing,
per-app networks, no-new-privileges, capped json-file logs, env via root-only
env-file, digest-resolved images, `hosty.managed` labels on everything.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.core.errors import AppError
from app.system import runner

MANAGED_LABEL = "hosty.managed=1"
APP_ID_LABEL = "hosty.app-id"

# Conservative subset of the Docker image reference grammar:
# [registry[:port]/]repo[/repo...][:tag][@digest]. Rejects whitespace, shell
# metacharacters and a leading dash by construction.
IMAGE_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]*)"  # first path component (or registry host)
    r"(?::[0-9]{1,5})?"  # optional registry :port
    r"(?:/[a-zA-Z0-9._-]+)*"  # optional extra path components
    r"(?::[a-zA-Z0-9._-]{1,128})?"  # optional :tag
    r"(?:@sha256:[a-f0-9]{64})?$"  # optional @digest
)
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")  # container/network names we derive


class DockerError(AppError):
    status_code = 502
    code = "docker_error"


class DockerUnavailableError(DockerError):
    code = "docker_unavailable"


class InvalidDockerArgError(runner.InvalidCommandError):
    pass


@dataclass(frozen=True)
class ContainerState:
    exists: bool
    status: str  # created | running | paused | restarting | exited | dead | absent
    running: bool
    exit_code: int | None
    health: str | None  # healthy | unhealthy | starting | None (no HEALTHCHECK)


@dataclass(frozen=True)
class ContainerSpec:
    """Everything `docker run` needs for one app container. ORM-free."""

    name: str  # hosty-app-<id>, derived by the service layer
    image: str  # digest-pinned ref whenever available
    internal_port: int
    host_port: int  # panel-allocated; published on 127.0.0.1 ONLY
    network: str  # per-app bridge network, never the default bridge
    env_file: str | None = None  # root-only file; never -e argv
    volumes: tuple[tuple[str, str], ...] = ()  # (host_dir, mount_path)
    memory_mb: int | None = None
    cpu_percent: int | None = None  # 100 = one full core
    labels: tuple[tuple[str, str], ...] = ()


def container_name(app_id: int) -> str:
    return f"hosty-app-{int(app_id)}"


def network_name(app_id: int) -> str:
    return f"hosty-net-{int(app_id)}"


# --- validation ----------------------------------------------------------------


def validate_image_ref(image: str) -> str:
    if not isinstance(image, str) or len(image) > 512 or not IMAGE_RE.fullmatch(image):
        raise InvalidDockerArgError(f"Invalid image reference: {image!r}")
    return image


def validate_name(name: str) -> str:
    if not isinstance(name, str) or len(name) > 128 or not NAME_RE.fullmatch(name):
        raise InvalidDockerArgError(f"Invalid docker object name: {name!r}")
    return name


def validate_port(port: int) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise InvalidDockerArgError(f"Invalid port: {port!r}")
    return port


def validate_env_key(key: str) -> str:
    if not isinstance(key, str) or len(key) > 128 or not ENV_KEY_RE.fullmatch(key):
        raise InvalidDockerArgError(f"Invalid environment variable name: {key!r}")
    return key


def validate_mount_path(path: str) -> str:
    """Container-side mount path: absolute, no traversal, no `:` (the -v
    separator), no NUL/newline."""
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 255
        or ":" in path
        or "\x00" in path
        or "\n" in path
        or ".." in path.split("/")
    ):
        raise InvalidDockerArgError(f"Invalid container mount path: {path!r}")
    return path.rstrip("/") or "/"


# --- argv builders (pure) --------------------------------------------------------


def build_pull_argv(image: str) -> list[str]:
    return ["docker", "image", "pull", "--quiet", validate_image_ref(image)]


def build_image_digest_argv(image: str) -> list[str]:
    return [
        "docker",
        "image",
        "inspect",
        "--format",
        "{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}",
        validate_image_ref(image),
    ]


def build_run_argv(spec: ContainerSpec) -> list[str]:
    argv = [
        "docker",
        "container",
        "run",
        "--detach",
        "--name",
        validate_name(spec.name),
        "--restart",
        "unless-stopped",
        "--network",
        validate_name(spec.network),
        "--security-opt",
        "no-new-privileges",
        "--label",
        MANAGED_LABEL,
        # A runaway container must not fill the host disk with logs.
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=3",
        # Loopback ONLY: a bare -p DNATs around ufw and exposes the app port
        # to the world even when the firewall says otherwise.
        "--publish",
        f"127.0.0.1:{validate_port(spec.host_port)}:{validate_port(spec.internal_port)}",
    ]
    for key, value in spec.labels:
        validate_env_key(key.replace(".", "_").replace("-", "_"))
        if "\x00" in value or "\n" in value:
            raise InvalidDockerArgError("Invalid label value")
        argv += ["--label", f"{key}={value}"]
    if spec.env_file is not None:
        argv += ["--env-file", spec.env_file]
    for host_dir, mount_path in spec.volumes:
        if ":" in host_dir or not host_dir.startswith("/"):
            raise InvalidDockerArgError(f"Invalid volume host dir: {host_dir!r}")
        argv += ["--volume", f"{host_dir}:{validate_mount_path(mount_path)}"]
    if spec.memory_mb is not None:
        argv += ["--memory", f"{int(spec.memory_mb)}m"]
    if spec.cpu_percent is not None:
        argv += ["--cpus", f"{int(spec.cpu_percent) / 100:g}"]
    argv.append(validate_image_ref(spec.image))
    return argv


def build_start_argv(name: str) -> list[str]:
    return ["docker", "container", "start", validate_name(name)]


def build_stop_argv(name: str, *, timeout_seconds: int = 10) -> list[str]:
    return [
        "docker",
        "container",
        "stop",
        "--time",
        str(int(timeout_seconds)),
        validate_name(name),
    ]


def build_restart_argv(name: str, *, timeout_seconds: int = 10) -> list[str]:
    return [
        "docker",
        "container",
        "restart",
        "--time",
        str(int(timeout_seconds)),
        validate_name(name),
    ]


def build_rm_argv(name: str) -> list[str]:
    return ["docker", "container", "rm", "--force", validate_name(name)]


def build_state_argv(name: str) -> list[str]:
    return [
        "docker",
        "container",
        "inspect",
        "--format",
        "{{json .State}}",
        validate_name(name),
    ]


def build_logs_argv(name: str, *, tail: int = 200) -> list[str]:
    tail = max(1, min(int(tail), 5000))
    return [
        "docker",
        "container",
        "logs",
        "--tail",
        str(tail),
        "--timestamps",
        validate_name(name),
    ]


def build_network_create_argv(name: str) -> list[str]:
    return [
        "docker",
        "network",
        "create",
        "--driver",
        "bridge",
        "--label",
        MANAGED_LABEL,
        validate_name(name),
    ]


def build_network_rm_argv(name: str) -> list[str]:
    return ["docker", "network", "rm", validate_name(name)]


def build_ps_argv() -> list[str]:
    """All panel-managed containers (and only those), machine-readable."""
    return [
        "docker",
        "container",
        "ls",
        "--all",
        "--filter",
        f"label={MANAGED_LABEL}",
        "--format",
        "{{json .}}",
    ]


# --- parsers (pure) --------------------------------------------------------------


ABSENT = ContainerState(exists=False, status="absent", running=False, exit_code=None, health=None)


def parse_state(stdout: str) -> ContainerState:
    try:
        raw = json.loads(stdout.strip() or "{}")
    except json.JSONDecodeError:
        return ABSENT
    if not isinstance(raw, dict) or not raw.get("Status"):
        return ABSENT
    health = raw.get("Health")
    return ContainerState(
        exists=True,
        status=str(raw.get("Status", "unknown")),
        running=bool(raw.get("Running", False)),
        exit_code=int(raw["ExitCode"]) if raw.get("ExitCode") is not None else None,
        health=str(health["Status"]) if isinstance(health, dict) and health.get("Status") else None,
    )


def parse_digest(stdout: str) -> str | None:
    digest = stdout.strip()
    return digest if digest and "@sha256:" in digest else None


# --- async operations -------------------------------------------------------------


async def _run_or_raise(argv: list[str], what: str, *, timeout: float = 60) -> runner.CommandResult:
    try:
        result = await runner.run(argv, timeout=timeout)
    except runner.CommandNotFoundError as exc:
        raise DockerUnavailableError("Docker is not installed on this host") from exc
    if not result.ok:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise DockerError(f"{what} failed: {detail[:400]}")
    return result


async def pull_image(image: str) -> None:
    await _run_or_raise(build_pull_argv(image), "Image pull", timeout=600)


async def resolve_digest(image: str) -> str | None:
    """RepoDigest of a locally present image, e.g. `nginx@sha256:...`."""
    result = await _run_or_raise(build_image_digest_argv(image), "Image inspect", timeout=30)
    return parse_digest(result.stdout)


async def run_container(spec: ContainerSpec) -> None:
    await _run_or_raise(build_run_argv(spec), "Container start", timeout=120)


async def start_container(name: str) -> None:
    await _run_or_raise(build_start_argv(name), "Container start", timeout=60)


async def stop_container(name: str) -> None:
    await _run_or_raise(build_stop_argv(name), "Container stop", timeout=60)


async def restart_container(name: str) -> None:
    await _run_or_raise(build_restart_argv(name), "Container restart", timeout=60)


async def remove_container(name: str) -> None:
    """Force-remove; absent container is success (idempotent deletes)."""
    try:
        result = await runner.run(build_rm_argv(name), timeout=60)
    except runner.CommandNotFoundError as exc:
        raise DockerUnavailableError("Docker is not installed on this host") from exc
    if not result.ok and "No such container" not in result.stderr:
        detail = result.stderr.strip() or "unknown error"
        raise DockerError(f"Container remove failed: {detail[:400]}")


async def container_state(name: str) -> ContainerState:
    try:
        result = await runner.run(build_state_argv(name), timeout=30)
    except runner.CommandNotFoundError:
        return ABSENT
    if not result.ok:
        return ABSENT  # includes "No such container"
    return parse_state(result.stdout)


async def container_logs(name: str, *, tail: int = 200) -> str:
    result = await _run_or_raise(build_logs_argv(name, tail=tail), "Container logs", timeout=30)
    # Docker writes container stderr to our stderr even on success.
    return result.stdout + result.stderr


async def create_network(name: str) -> None:
    await _run_or_raise(build_network_create_argv(name), "Network create", timeout=60)


async def remove_network(name: str) -> None:
    """Absent network is success (idempotent deletes)."""
    try:
        result = await runner.run(build_network_rm_argv(name), timeout=60)
    except runner.CommandNotFoundError as exc:
        raise DockerUnavailableError("Docker is not installed on this host") from exc
    if not result.ok and "not found" not in result.stderr:
        detail = result.stderr.strip() or "unknown error"
        raise DockerError(f"Network remove failed: {detail[:400]}")
