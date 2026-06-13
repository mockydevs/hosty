"""Podman seam (v2/M2): exact argv via the tenant socket, total parsers
(garbage in, empty out — observers must never crash), graceful ps."""

from __future__ import annotations

import json

import pytest

from app.domain.validate import SpecValidationError
from app.system import podman, runner


def test_socket_url():
    assert podman.socket_url(5001) == "unix:/run/user/5001/podman/podman.sock"


@pytest.mark.parametrize("uid", [0, -3, True, "5001", None])
def test_bad_uid_rejected(uid):
    with pytest.raises(podman.InvalidPodmanArgError):
        podman.socket_url(uid)


def test_ps_argv():
    assert podman.build_ps_argv(5001) == [
        "podman",
        "--url",
        "unix:/run/user/5001/podman/podman.sock",
        "ps",
        "--all",
        "--filter",
        "label=hosty.managed=1",
        "--format",
        "json",
    ]


def test_pull_and_digest_argv():
    assert podman.build_pull_argv(5001, "nginx:1.27")[-1] == "docker.io/library/nginx:1.27"
    argv = podman.build_image_digest_argv(5001, "nginx:1.27")
    assert argv[3:5] == ["image", "inspect"]
    with pytest.raises(SpecValidationError):
        podman.build_pull_argv(5001, "-rm")


def test_exec_argv():
    argv = podman.build_exec_argv(5001, "blog-web", ["wp", "core", "version"])
    assert argv == [
        "podman",
        "--url",
        "unix:/run/user/5001/podman/podman.sock",
        "exec",
        "blog-web",
        "wp",
        "core",
        "version",
    ]
    with pytest.raises(SpecValidationError):
        podman.build_exec_argv(5001, "-evil", ["true"])
    with pytest.raises(podman.InvalidPodmanArgError):
        podman.build_exec_argv(5001, "blog-web", [])


def test_parse_ps_happy():
    payload = json.dumps(
        [
            {
                "Names": ["blog-web"],
                "Image": "nginx:1.27",
                "State": "running",
                "Labels": {"hosty.managed": "1", "hosty.stack": "blog"},
            },
            {
                "Names": ["blog-db"],
                "Image": "mariadb:11",
                "State": "exited",
                "Labels": {"hosty.managed": "1", "hosty.stack": "blog"},
            },
        ]
    )
    web, db = podman.parse_ps(payload)
    assert web == podman.PsContainer(
        name="blog-web", image="nginx:1.27", state="running", running=True, stack="blog"
    )
    assert db.running is False and db.stack == "blog"


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "not json",
        "{}",  # object, not array
        "[1, 2]",  # non-dict entries
        '[{"Names": []}]',  # no name
        '[{"Names": [3]}]',  # non-string name
        '[{"Names": ["x"], "Labels": "nope", "State": null}]',
    ],
)
def test_parse_ps_is_total(garbage):
    containers = podman.parse_ps(garbage)
    assert isinstance(containers, list)
    for c in containers:
        assert c.name and c.stack is None
        assert c.state == "unknown" and c.running is False


def test_parse_digest():
    assert podman.parse_digest("nginx@sha256:" + "a" * 64 + "\n") == "nginx@sha256:" + "a" * 64
    assert podman.parse_digest("") is None
    assert podman.parse_digest("nginx:latest") is None


async def test_ps_graceful_when_podman_missing(monkeypatch):
    async def boom(argv, **kwargs):
        raise runner.CommandNotFoundError("podman")

    monkeypatch.setattr(podman.runner, "run", boom)
    assert await podman.ps(5001) == []


async def test_ps_graceful_when_socket_down(monkeypatch):
    async def fail(argv, **kwargs):
        return runner.CommandResult(tuple(argv), 125, "", "connection refused", 1.0)

    monkeypatch.setattr(podman.runner, "run", fail)
    assert await podman.ps(5001) == []


async def test_pull_raises_with_detail(monkeypatch):
    async def fail(argv, **kwargs):
        return runner.CommandResult(tuple(argv), 125, "", "manifest unknown", 1.0)

    monkeypatch.setattr(podman.runner, "run", fail)
    with pytest.raises(podman.PodmanError, match="manifest unknown"):
        await podman.pull_image(5001, "nginx:nope")


async def test_resolve_digest_and_exec_passthrough(monkeypatch):
    async def ok(argv, **kwargs):
        if "inspect" in argv:
            return runner.CommandResult(tuple(argv), 0, "nginx@sha256:" + "b" * 64, "", 1.0)
        return runner.CommandResult(tuple(argv), 3, "out", "err", 1.0)

    monkeypatch.setattr(podman.runner, "run", ok)
    assert await podman.resolve_digest(5001, "nginx:1.27") == "nginx@sha256:" + "b" * 64
    # exec returns the raw result — non-zero exit is the blueprint's call.
    result = await podman.exec_in(5001, "blog-web", ["wp", "--version"])
    assert result.returncode == 3 and result.stdout == "out"


async def test_exec_raises_when_podman_missing(monkeypatch):
    async def boom(argv, **kwargs):
        raise runner.CommandNotFoundError("podman")

    monkeypatch.setattr(podman.runner, "run", boom)
    with pytest.raises(podman.PodmanUnavailableError):
        await podman.exec_in(5001, "blog-web", ["true"])
