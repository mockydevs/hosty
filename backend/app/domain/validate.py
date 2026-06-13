"""Validation grammar — the single source of truth (v2/M1, ADR-013).

Every value that can reach an argv, a unit file, or a path is validated
here against an allowlist grammar. A leading `-` can never survive
(argv option injection), nor can NUL/newline (unit-file and env-file
injection), nor `:` in mount components (the -v separator).

system/docker.py (until its M6 deletion) and all v2 adapters consume these;
nothing else may define input grammars.
"""

from __future__ import annotations

import re

# Conservative subset of the OCI image reference grammar:
# [registry[:port]/]repo[/repo...][:tag][@sha256:digest]
IMAGE_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]*)"  # first path component (or registry host)
    r"(?::[0-9]{1,5})?"  # optional registry :port
    r"(?:/[a-zA-Z0-9._-]+)*"  # optional extra path components
    r"(?::[a-zA-Z0-9._-]{1,128})?"  # optional :tag
    r"(?:@sha256:[a-f0-9]{64})?$"  # optional @digest
)
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Derived object names (containers, networks, unit base names).
OBJECT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
# User-chosen short names (stacks, services, volumes): strict slug.
SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
GIT_HTTPS_RE = re.compile(
    r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?/[A-Za-z0-9._~/-]+(?:\.git)?$"
)
GIT_SSH_RE = re.compile(
    r"^(?:ssh://)?git@[A-Za-z0-9.-]+(?::[0-9]{1,5})?[:/][A-Za-z0-9._~/-]+(?:\.git)?$"
)
GIT_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")


class SpecValidationError(ValueError):
    """A value failed the domain grammar before reaching any host surface."""


def validate_image_ref(image: str) -> str:
    if not isinstance(image, str) or len(image) > 512 or not IMAGE_RE.fullmatch(image):
        raise SpecValidationError(f"Invalid image reference: {image!r}")
    return image


def validate_git_repo(repo: str) -> str:
    if (
        not isinstance(repo, str)
        or len(repo) > 1024
        or "\x00" in repo
        or "\n" in repo
        or not (GIT_HTTPS_RE.fullmatch(repo) or GIT_SSH_RE.fullmatch(repo))
    ):
        raise SpecValidationError(f"Invalid Git repository URL: {repo!r}")
    return repo


def validate_git_ref(ref: str) -> str:
    if (
        not isinstance(ref, str)
        or not GIT_REF_RE.fullmatch(ref)
        or ".." in ref
        or "@{" in ref
        or ref.endswith(("/", "."))
    ):
        raise SpecValidationError(f"Invalid Git branch or ref: {ref!r}")
    return ref


def validate_object_name(name: str) -> str:
    if not isinstance(name, str) or len(name) > 128 or not OBJECT_NAME_RE.fullmatch(name):
        raise SpecValidationError(f"Invalid object name: {name!r}")
    return name


def validate_slug(value: str, *, what: str = "name") -> str:
    if not isinstance(value, str) or not SLUG_RE.fullmatch(value):
        raise SpecValidationError(
            f"Invalid {what}: {value!r} (1-32 lowercase letters, digits, or "
            "hyphens; no leading/trailing hyphen)"
        )
    return value


def validate_port(port: int) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise SpecValidationError(f"Invalid port: {port!r}")
    return port


def validate_env_key(key: str) -> str:
    if not isinstance(key, str) or len(key) > 128 or not ENV_KEY_RE.fullmatch(key):
        raise SpecValidationError(f"Invalid environment variable name: {key!r}")
    return key


def validate_env_value(key: str, value: str) -> str:
    if not isinstance(value, str) or "\x00" in value or "\n" in value or len(value) > 4096:
        raise SpecValidationError(f"Invalid value for environment variable {key}")
    return value


def validate_mount_path(path: str) -> str:
    """Container-side mount path: absolute, no traversal, no `:` (the volume
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
        raise SpecValidationError(f"Invalid container mount path: {path!r}")
    return path.rstrip("/") or "/"


def validate_domain_name(domain: str) -> str:
    """Endpoint domains. Full IDNA handling lives at the API boundary
    (services/sites.validate_domain until M6); the domain grammar here is the
    structural backstop for anything that reaches a route or unit."""
    if (
        not isinstance(domain, str)
        or not 1 <= len(domain) <= 253
        or not re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", domain)
        or ".." in domain
    ):
        raise SpecValidationError(f"Invalid domain: {domain!r}")
    return domain
