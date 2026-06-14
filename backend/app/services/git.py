import os
import tempfile
import yaml
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel

from app.system.runner import CommandNotFoundError, CommandTimeoutError, run

log = structlog.get_logger("hosty.git")


class GitAnalysisResult(BaseModel):
    has_dockerfile: bool
    has_compose: bool
    compose_services: list[str] = []
    env_keys: list[str] = []
    compose_file_content: str | None = None


@asynccontextmanager
async def _clone_shallow(url: str, branch: str) -> AsyncGenerator[str, None]:
    """Clones a single branch of a repository into a temporary directory."""
    with tempfile.TemporaryDirectory(prefix="hosty-git-") as tmpdir:
        cmd = [
            "git", "clone", "--depth", "1", "--branch", branch,
            "--single-branch", url, tmpdir,
        ]
        try:
            result = await run(cmd, timeout=60.0)
        except CommandTimeoutError as exc:
            raise ValueError("Repository clone timed out after 60 seconds") from exc
        except CommandNotFoundError as exc:
            raise ValueError("git is not installed on this server") from exc
        if not result.ok:
            stderr_snippet = (result.stderr or "").strip()[:500]
            log.error("git_clone_failed", url=url, branch=branch, stderr=stderr_snippet)
            raise ValueError(f"Failed to clone repository: {stderr_snippet}")
        yield tmpdir


def _extract_env_keys(compose_data: dict[str, Any]) -> list[str]:
    """Extracts all variable interpolations from a compose dictionary."""
    keys: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "environment":
                    if isinstance(v, dict):
                        keys.update(v.keys())
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, str) and "=" in item:
                                keys.add(item.split("=")[0])
                            elif isinstance(item, str):
                                keys.add(item)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            # Very basic extraction of ${VAR} and $VAR
            import re
            matches = re.findall(r"\$\{([a-zA-Z0-9_]+)[^\}]*\}|\$([a-zA-Z0-9_]+)", node)
            for m in matches:
                keys.add(m[0] or m[1])

    walk(compose_data)
    return sorted(list(keys))


async def analyze_repo(url: str, branch: str) -> GitAnalysisResult:
    """Clones a repo locally, detects files, and extracts services/env."""
    async with _clone_shallow(url, branch) as repo_dir:
        has_dockerfile = os.path.exists(os.path.join(repo_dir, "Dockerfile"))
        
        compose_paths = [
            "docker-compose.yml",
            "docker-compose.yaml",
            "compose.yml",
            "compose.yaml",
        ]
        compose_file = None
        for path in compose_paths:
            if os.path.exists(os.path.join(repo_dir, path)):
                compose_file = os.path.join(repo_dir, path)
                break
                
        has_compose = compose_file is not None
        services: list[str] = []
        env_keys: list[str] = []

        compose_content = None
        if has_compose:
            try:
                with open(compose_file, "r") as f:
                    compose_content = f.read()
                    data = yaml.safe_load(compose_content)
                
                if isinstance(data, dict) and "services" in data:
                    services = list(data["services"].keys())
                    env_keys = _extract_env_keys(data)
            except Exception as e:
                log.warning("compose_parse_failed", path=compose_file, exc_info=e)

        return GitAnalysisResult(
            has_dockerfile=has_dockerfile,
            has_compose=has_compose,
            compose_services=services,
            env_keys=env_keys,
            compose_file_content=compose_content,
        )
