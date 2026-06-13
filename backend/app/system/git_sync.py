"""Systemd service for syncing Git repositories into tenant workspaces."""

from app.domain.specs import StackSpec

def git_sync_unit(stack: StackSpec) -> str:
    """Generates a systemd service that syncs the repository to local disk."""
    # We only need one sync per stack, using the first service's repo.
    # In a full compose scenario, the whole stack shares one repo.
    svc = next((s for s in stack.services if s.build_repo), None)
    if not svc:
        return ""
        
    repo = svc.build_repo
    branch = svc.build_branch or "main"
    # Local path inside the tenant's directory
    # %h resolves to the user's home directory (e.g., /opt/hosty/data/hosty-t-1)
    workspace = f"%h/stacks/{stack.name}/src"
    
    return "\n".join([
        "[Unit]",
        f"Description=Sync Git Repository for {stack.name}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=oneshot",
        # Clear existing source to ensure clean state (or use git pull)
        f"ExecStartPre=-/usr/bin/rm -rf {workspace}",
        f"ExecStart=/usr/bin/git clone --depth 1 --branch {branch} --single-branch {repo} {workspace}",
        # Keep the workspace around after service exits
        "RemainAfterExit=yes",
        "",
        "[Install]",
        "WantedBy=default.target"
    ])
