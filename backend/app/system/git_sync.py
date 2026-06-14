"""Systemd service for syncing Git repositories into tenant workspaces."""

from app.domain.specs import StackSpec


def git_sync_unit(stack: StackSpec, clone_url: str | None = None) -> str:
    """Generates a systemd service that syncs the repository to local disk.

    `clone_url` overrides the URL used in the ExecStart git clone command —
    pass an authenticated URL (https://x-access-token:{token}@github.com/…)
    for private repos.  The token is NOT in the spec_hash so reconvergence
    with a refreshed token rewrites the unit without triggering extra cycles.
    """
    svc = next((s for s in stack.services if s.build_repo), None)
    if not svc:
        return ""

    repo = clone_url or svc.build_repo
    branch = svc.build_branch or "main"
    workspace = f"%h/stacks/{stack.name}/src"

    return "\n".join([
        "[Unit]",
        f"Description=Sync Git repository for stack {stack.name}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        f"ExecStartPre=-/usr/bin/rm -rf {workspace}",
        f"ExecStart=/usr/bin/git clone --depth 1 --branch {branch} --single-branch {repo} {workspace}",
        "RemainAfterExit=yes",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])
