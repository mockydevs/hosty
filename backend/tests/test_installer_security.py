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


def test_bootstrap_is_sha_pinned_truncation_safe_and_defers_to_verified_checkout():
    bootstrap = (ROOT / "installer" / "get.sh").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    # Same immutable-SHA requirement as the installer proper.
    assert "^[0-9a-f]{40}$" in bootstrap
    # All logic in main(), invoked on the last line: a truncated download
    # parses but executes nothing.
    assert bootstrap.rstrip().endswith('main "$@"')
    # The checkout must be verified before handing off to the real installer.
    assert "rev-parse HEAD" in bootstrap
    assert "installer/install.sh" in bootstrap
    # The README one-liner pins the bootstrap URL to the install SHA — never
    # a mutable branch — and enforces HTTPS-only TLS 1.2+.
    assert "$HOSTY_REF/installer/get.sh" in readme
    assert "raw.githubusercontent.com/mockydevs/hosty/main" not in readme
    assert "--proto '=https' --tlsv1.2" in readme


def test_service_is_loopback_only_and_proxy_headers_are_trusted_locally():
    unit = (ROOT / "installer" / "systemd" / "hosty.service").read_text(encoding="utf-8")
    assert "--host 127.0.0.1" in unit
    assert "--forwarded-allow-ips=127.0.0.1" in unit
    assert "--host 0.0.0.0" not in unit
