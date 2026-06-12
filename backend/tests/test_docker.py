"""Docker seam: validators reject injection, argv builders are exact, parsers
are total (never raise on engine garbage)."""

from __future__ import annotations

import pytest

from app.system import docker
from app.system.docker import ContainerSpec


def spec(**overrides) -> ContainerSpec:
    base = dict(
        name="hosty-app-7",
        image="nginx:1.27",
        internal_port=3000,
        host_port=20100,
        network="hosty-net-7",
    )
    base.update(overrides)
    return ContainerSpec(**base)


# --- validators -------------------------------------------------------------------


@pytest.mark.parametrize(
    "image",
    [
        "nginx",
        "nginx:1.27",
        "library/nginx:latest",
        "ghcr.io/org/app:v1.2.3",
        "registry.example.com:5000/team/app:tag",
        "nginx@sha256:" + "a" * 64,
        "ghcr.io/org/app:v1@sha256:" + "0" * 64,
    ],
)
def test_valid_image_refs(image):
    assert docker.validate_image_ref(image) == image


@pytest.mark.parametrize(
    "image",
    [
        "",
        "-nginx",  # argv option injection
        "--privileged",
        "nginx; rm -rf /",
        "nginx latest",
        "NGINX:latest",  # uppercase repo
        "nginx:tag with space",
        "nginx\nlatest",
        "nginx@sha256:short",
        "a" * 600,
    ],
)
def test_invalid_image_refs_rejected(image):
    with pytest.raises(docker.InvalidDockerArgError):
        docker.validate_image_ref(image)


@pytest.mark.parametrize("name", ["-evil", "evil name", "", "UPPER", "x" * 200])
def test_invalid_names_rejected(name):
    with pytest.raises(docker.InvalidDockerArgError):
        docker.validate_name(name)


@pytest.mark.parametrize("port", [0, -1, 65536, "80", None, True])
def test_invalid_ports_rejected(port):
    with pytest.raises(docker.InvalidDockerArgError):
        docker.validate_port(port)


@pytest.mark.parametrize("key", ["1BAD", "-FLAG", "WITH SPACE", "", "WITH=EQ"])
def test_invalid_env_keys_rejected(key):
    with pytest.raises(docker.InvalidDockerArgError):
        docker.validate_env_key(key)


@pytest.mark.parametrize(
    "path", ["relative/path", "/with:colon", "/../escape", "/a/../../b", "", "/x\n"]
)
def test_invalid_mount_paths_rejected(path):
    with pytest.raises(docker.InvalidDockerArgError):
        docker.validate_mount_path(path)


# --- argv builders ----------------------------------------------------------------


def test_run_argv_hardening_invariants():
    argv = docker.build_run_argv(
        spec(
            env_file="/var/lib/hosty/apps/7/env",
            volumes=(("/var/lib/hosty/apps/7/volumes/data", "/data"),),
            memory_mb=512,
            cpu_percent=150,
            labels=(("hosty.app-id", "7"),),
        )
    )
    joined = " ".join(argv)
    # Loopback-only publishing — never a bare -p.
    assert "--publish" in argv
    assert "127.0.0.1:20100:3000" in argv
    # Privilege and disk-fill guards.
    assert "no-new-privileges" in argv
    assert "max-size=10m" in joined
    # Per-app network, managed labels, env via file (never -e).
    assert argv[argv.index("--network") + 1] == "hosty-net-7"
    assert docker.MANAGED_LABEL in argv
    assert "hosty.app-id=7" in argv
    assert "--env-file" in argv
    assert "-e" not in argv
    # Limits and image last.
    assert argv[argv.index("--memory") + 1] == "512m"
    assert argv[argv.index("--cpus") + 1] == "1.5"
    assert argv[-1] == "nginx:1.27"


def test_run_argv_rejects_bad_volume_host_dir():
    with pytest.raises(docker.InvalidDockerArgError):
        docker.build_run_argv(spec(volumes=(("relative/dir", "/data"),)))
    with pytest.raises(docker.InvalidDockerArgError):
        docker.build_run_argv(spec(volumes=(("/dir:with:colons", "/data"),)))


def test_lifecycle_argvs():
    assert docker.build_stop_argv("hosty-app-7") == [
        "docker",
        "container",
        "stop",
        "--time",
        "10",
        "hosty-app-7",
    ]
    assert docker.build_rm_argv("hosty-app-7") == [
        "docker",
        "container",
        "rm",
        "--force",
        "hosty-app-7",
    ]
    assert docker.build_network_create_argv("hosty-net-7") == [
        "docker",
        "network",
        "create",
        "--driver",
        "bridge",
        "--label",
        docker.MANAGED_LABEL,
        "hosty-net-7",
    ]
    assert "--tail" in docker.build_logs_argv("hosty-app-7", tail=50)


def test_logs_tail_is_clamped():
    argv = docker.build_logs_argv("hosty-app-7", tail=10_000_000)
    assert argv[argv.index("--tail") + 1] == "5000"


def test_ps_argv_only_targets_managed_containers():
    argv = docker.build_ps_argv()
    assert f"label={docker.MANAGED_LABEL}" in argv


# --- parsers ----------------------------------------------------------------------


def test_parse_state_running_with_health():
    state = docker.parse_state(
        '{"Status":"running","Running":true,"ExitCode":0,"Health":{"Status":"healthy"}}'
    )
    assert state.exists and state.running
    assert state.status == "running"
    assert state.health == "healthy"


def test_parse_state_exited():
    state = docker.parse_state('{"Status":"exited","Running":false,"ExitCode":137}')
    assert state.exists and not state.running
    assert state.exit_code == 137
    assert state.health is None


@pytest.mark.parametrize("stdout", ["", "not json", "[]", "{}", "null"])
def test_parse_state_garbage_is_absent(stdout):
    assert docker.parse_state(stdout) == docker.ABSENT


def test_parse_digest():
    assert docker.parse_digest("nginx@sha256:" + "a" * 64 + "\n") == "nginx@sha256:" + "a" * 64
    assert docker.parse_digest("") is None
    assert docker.parse_digest("nginx:latest") is None


def test_derived_names():
    assert docker.container_name(7) == "hosty-app-7"
    assert docker.network_name(7) == "hosty-net-7"
