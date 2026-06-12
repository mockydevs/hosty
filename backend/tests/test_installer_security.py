from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_installer_uses_local_checksum_verified_tools():
    install = (ROOT / "installer" / "install.sh").read_text(encoding="utf-8")
    tools = (ROOT / "installer" / "install-tools.sh").read_text(encoding="utf-8")
    # Third-party tools (uv, node, wp-cli, ...) come from pinned versions with
    # sha256 verification — never piped from a vendor URL into a shell.
    assert "install-tools.sh" in install
    assert "sha256sum --check" in tools
    assert "curl |" not in install


def test_docker_daemon_is_hardened_in_provisioner():
    provision = (ROOT / "installer" / "provision.sh").read_text(encoding="utf-8")
    # Published ports default to loopback: Docker's iptables DNAT bypasses ufw,
    # so this is the only line standing between a tenant port and the internet.
    assert '"ip": "127.0.0.1"' in provision
    # Container root must never be host root; flipping this later re-chowns
    # every volume, so it is a day-one default.
    assert '"userns-remap": "default"' in provision
    assert '"live-restore": true' in provision
    assert '"no-new-privileges": true' in provision


def test_service_trusts_proxy_headers_from_localhost_only():
    unit = (ROOT / "installer" / "systemd" / "hosty.service").read_text(encoding="utf-8")
    # Panel binds all interfaces for direct http://IP:8800 access; forwarded
    # headers are still only trusted from the local Caddy proxy.
    assert "--host 0.0.0.0" in unit
    assert "--forwarded-allow-ips=127.0.0.1" in unit
