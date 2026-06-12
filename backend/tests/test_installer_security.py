from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_installer_requires_immutable_commit_and_local_verified_tools():
    install = (ROOT / "installer" / "install.sh").read_text(encoding="utf-8")
    update = (ROOT / "installer" / "update.sh").read_text(encoding="utf-8")
    tools = (ROOT / "installer" / "install-tools.sh").read_text(encoding="utf-8")
    assert "^[0-9a-f]{40}$" in install
    assert "^[0-9a-f]{40}$" in update
    assert "install-tools.sh" in install
    assert "sha256sum --check" in tools
    assert "curl |" not in install


def test_service_is_loopback_only_and_proxy_headers_are_trusted_locally():
    unit = (ROOT / "installer" / "systemd" / "hosty.service").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in unit
    assert "--forwarded-allow-ips=127.0.0.1" in unit
    assert "--host 0.0.0.0" not in unit
