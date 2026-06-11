"""Domain validation matrix + site-user derivation."""

from __future__ import annotations

import pytest

from app.services.sites import DomainValidationError, derive_site_user, validate_domain
from app.system.users import validate_site_username


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("example.com", "example.com"),
        ("EXAMPLE.COM", "example.com"),
        (" example.com ", "example.com"),
        ("example.com.", "example.com"),
        ("sub.deep.example.co.uk", "sub.deep.example.co.uk"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
        ("bücher.example", "xn--bcher-kva.example"),  # unicode → punycode
        ("münchen.de", "xn--mnchen-3ya.de"),
        ("a-b.example", "a-b.example"),
        ("123.example", "123.example"),
    ],
)
def test_valid_domains(raw, expected):
    assert validate_domain(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "example",  # no dot
        "localhost",
        "-bad.example",  # leading hyphen
        "bad-.example",  # trailing hyphen
        "exa mple.com",  # space
        "example.com/path",  # path
        "http://example.com",  # scheme
        "example..com",  # empty label
        "*.example.com",  # wildcard
        "example.com;rm -rf /",  # injection attempt
        "exam\nple.com",  # newline
        "a" * 64 + ".example",  # label too long
        "x." * 130 + "com",  # domain too long
    ],
)
def test_invalid_domains(raw):
    with pytest.raises(DomainValidationError):
        validate_domain(raw)


def test_derive_site_user_is_valid_and_deterministic():
    user = derive_site_user("example.com")
    assert validate_site_username(user) == user
    assert user == derive_site_user("example.com")
    assert user != derive_site_user("example.org")


def test_derive_site_user_handles_long_and_unicode_domains():
    for domain in [validate_domain("bücher.example"), "a-very-long-subdomain.deep.example.com"]:
        user = derive_site_user(domain)
        assert validate_site_username(user) == user
        assert len(user) <= 32
