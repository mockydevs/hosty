"""Systemd service for syncing Git repositories into tenant workspaces."""

from app.domain.specs import StackSpec
from app.system.quadlet import MANAGED_HEADER


def git_sync_unit(stack: StackSpec, token: str | None = None) -> str:
    """Generates a systemd service that syncs the repository to local disk.

    `token` is an optional GitHub App installation token. When provided,
    it is injected via git's insteadOf configuration. This allows submodules
    on the same host to automatically authenticate without persisting the
    token into the workspace's .git/config.
    """
    svc = next((s for s in stack.services if s.build_repo), None)
    if not svc:
        return ""

    repo = svc.build_repo
    branch = svc.build_branch or "main"
    workspace = f"%h/stacks/{stack.name}/src"
    
    git_bin = "/usr/bin/git"
    if token and repo.startswith("https://"):
        from urllib.parse import urlparse
        parsed = urlparse(repo)
        scheme = parsed.scheme
        host = parsed.netloc
        git_bin = f"/usr/bin/git -c url.{scheme}://x-access-token:{token}@{host}/.insteadOf={scheme}://{host}/"

    build_services = [
        f"{stack.name}-{s.name}-build.service"
        for s in stack.services
        if s.build_repo
    ]
    part_of_lines = [f"PartOf={bs}" for bs in build_services]

    return "\n".join([
        MANAGED_HEADER,
        f"# hosty-stack={stack.name}",
        "",
        "[Unit]",
        f"Description=Sync Git repository for stack {stack.name}",
        "After=network-online.target",
        "Wants=network-online.target",
        *part_of_lines,
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStartPre=-/usr/bin/rm -rf {workspace}",
        f"ExecStart={git_bin} clone --depth 1 --branch {branch} --single-branch --recurse-submodules --shallow-submodules {repo} {workspace}",
        "RemainAfterExit=yes",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])
