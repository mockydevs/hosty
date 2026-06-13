"""Domain validation grammar (v2/M1): every accept and reject arm.
Injection vectors (leading `-`, NUL, newline, `:` in mounts) must be
rejected by construction."""

from __future__ import annotations

import pytest

from app.domain import validate as v


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("nginx", "docker.io/library/nginx"),
        ("nginx:1.27-alpine", "docker.io/library/nginx:1.27-alpine"),
        ("library/nginx:latest", "docker.io/library/nginx:latest"),
        ("docker.io/library/nginx:1.27", "docker.io/library/nginx:1.27"),
        ("ghcr.io/acme/app:v1.2.3", "ghcr.io/acme/app:v1.2.3"),
        ("registry.example.com:5000/team/app:tag", "registry.example.com:5000/team/app:tag"),
        ("nginx@sha256:" + "a" * 64, "docker.io/library/nginx@sha256:" + "a" * 64),
        (
            "docker.io/library/nginx:1.27@sha256:" + "0" * 64,
            "docker.io/library/nginx:1.27@sha256:" + "0" * 64,
        ),
    ],
)
def test_valid_image_refs(image, expected):
    assert v.validate_image_ref(image) == expected


@pytest.mark.parametrize(
    "image",
    [
        "",
        "-nginx",  # option injection
        "Nginx",  # uppercase first component
        "nginx:tag with space",
        "nginx:" + "t" * 129,  # tag too long
        "nginx@sha256:short",
        "nginx@sha1:" + "a" * 64,
        "a" * 513,  # total length
        "nginx\n:latest",
        None,
        42,
    ],
)
def test_invalid_image_refs(image):
    with pytest.raises(v.SpecValidationError):
        v.validate_image_ref(image)


@pytest.mark.parametrize(
    "repo",
    [
        "https://github.com/example/app.git",
        "https://gitlab.com/group/subgroup/app.git",
        "git@github.com:example/private-app.git",
        "ssh://git@git.example.com:2222/team/app.git",
    ],
)
def test_valid_git_repositories(repo):
    assert v.validate_git_repo(repo) == repo


@pytest.mark.parametrize(
    "repo",
    [
        "",
        "http://github.com/example/app.git",
        "file:///etc/passwd",
        "https://github.com/example/app.git\nInjected=true",
        "-https://github.com/example/app.git",
        "x" * 1025,
        None,
    ],
)
def test_invalid_git_repositories(repo):
    with pytest.raises(v.SpecValidationError):
        v.validate_git_repo(repo)


@pytest.mark.parametrize("ref", ["main", "release/v1.2", "feature_1", "abc-123"])
def test_valid_git_refs(ref):
    assert v.validate_git_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    ["", "-main", "feature..bad", "main@{1}", "bad ref", "main/", "main.", "a" * 256, None],
)
def test_invalid_git_refs(ref):
    with pytest.raises(v.SpecValidationError):
        v.validate_git_ref(ref)


@pytest.mark.parametrize("name", ["a", "web", "my-app2", "a" + "b" * 30 + "c"])
def test_valid_slugs(name):
    assert v.validate_slug(name) == name


@pytest.mark.parametrize(
    "name",
    ["", "-web", "web-", "WEB", "we_b", "a" * 33, "we b", "web\n", None, 3],
)
def test_invalid_slugs(name):
    with pytest.raises(v.SpecValidationError) as exc:
        v.validate_slug(name, what="service name")
    assert "service name" in str(exc.value)


@pytest.mark.parametrize("name", ["nginx", "hosty-stack.web", "a0_b-c.d"])
def test_valid_object_names(name):
    assert v.validate_object_name(name) == name


@pytest.mark.parametrize("name", ["", "-x", "_x", ".x", "X", "a" * 129, "a b", None])
def test_invalid_object_names(name):
    with pytest.raises(v.SpecValidationError):
        v.validate_object_name(name)


@pytest.mark.parametrize("port", [1, 80, 65535])
def test_valid_ports(port):
    assert v.validate_port(port) == port


@pytest.mark.parametrize("port", [0, -1, 65536, "80", 80.0, True, None])
def test_invalid_ports(port):
    with pytest.raises(v.SpecValidationError):
        v.validate_port(port)


@pytest.mark.parametrize("key", ["A", "_private", "DB_HOST", "k2"])
def test_valid_env_keys(key):
    assert v.validate_env_key(key) == key


@pytest.mark.parametrize("key", ["", "2X", "A-B", "A B", "A\n", "A" * 129, None, 1])
def test_invalid_env_keys(key):
    with pytest.raises(v.SpecValidationError):
        v.validate_env_key(key)


def test_valid_env_values():
    assert v.validate_env_value("K", "") == ""
    assert v.validate_env_value("K", "hello world -- $tuff = fine") != ""


@pytest.mark.parametrize("value", ["a\nb", "a\x00b", "x" * 4097, None, 5])
def test_invalid_env_values(value):
    with pytest.raises(v.SpecValidationError) as exc:
        v.validate_env_value("MY_KEY", value)
    assert "MY_KEY" in str(exc.value)


@pytest.mark.parametrize(
    ("path", "expected"),
    [("/data", "/data"), ("/var/www/", "/var/www"), ("/", "/")],
)
def test_valid_mount_paths(path, expected):
    assert v.validate_mount_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "data",  # relative
        "/data:ro",  # `:` is the -v separator
        "/data/../etc",  # traversal
        "/data\n",
        "/data\x00",
        "/" + "a" * 255,
        "",
        None,
        7,
    ],
)
def test_invalid_mount_paths(path):
    with pytest.raises(v.SpecValidationError):
        v.validate_mount_path(path)


@pytest.mark.parametrize("domain", ["example.com", "a.b-c.example.io", "x", "xn--bcher-kva.ch"])
def test_valid_domains(domain):
    assert v.validate_domain_name(domain) == domain


@pytest.mark.parametrize(
    "domain",
    ["", "-x.com", "x-.com" + "\n", "a..b", "A.com", "a" * 254, ".com", None, 9],
)
def test_invalid_domains(domain):
    with pytest.raises(v.SpecValidationError):
        v.validate_domain_name(domain)
