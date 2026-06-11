from __future__ import annotations

import httpx
import pytest

from app.core.errors import ConflictError
from app.services import dns

ZONE = "example.com"


# --- zone names --------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("example.com", "example.com."),
        ("Example.COM.", "example.com."),
        ("sub.example.co.uk", "sub.example.co.uk."),
    ],
)
def test_zone_names_valid(raw, expected):
    assert dns.validate_zone_name(raw) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "localhost",
        "ex ample.com",
        "-bad.com",
        "exa_mple.com",
        "a.",
        "a..b.com",
        "x" * 254 + ".com",
        "example.com; rm -rf /",
        "exa\nmple.com",
    ],
)
def test_zone_names_invalid(bad):
    with pytest.raises(dns.DNSValidationError):
        dns.validate_zone_name(bad)


# --- record names ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("@", "example.com."),
        ("", "example.com."),
        ("www", "www.example.com."),
        ("a.b", "a.b.example.com."),
        ("*.app", "*.app.example.com."),
        ("_dmarc", "_dmarc.example.com."),
        ("_sip._tcp", "_sip._tcp.example.com."),
        ("mail.example.com.", "mail.example.com."),
        ("example.com.", "example.com."),
    ],
)
def test_record_names_valid(name, expected):
    assert dns.normalize_record_name(name, ZONE) == expected


@pytest.mark.parametrize("bad", ["evil.org.", "a b", "a..b", "x.*.y", "-x", "w\nw"])
def test_record_names_invalid(bad):
    with pytest.raises(dns.DNSValidationError):
        dns.normalize_record_name(bad, ZONE)


# --- record content ----------------------------------------------------------------


@pytest.mark.parametrize(
    "rtype,content,expected",
    [
        ("A", "192.0.2.1", "192.0.2.1"),
        ("AAAA", "2001:db8::1", "2001:db8::1"),
        ("CNAME", "Target.Example.com", "target.example.com."),
        ("NS", "ns1.example.com.", "ns1.example.com."),
        ("MX", "10 mail.example.com", "10 mail.example.com."),
        ("SRV", "10 5 5060 sip.example.com", "10 5 5060 sip.example.com."),
        ("TXT", "v=spf1 mx ~all", '"v=spf1 mx ~all"'),
        ("TXT", '"already quoted"', '"already quoted"'),
        ("TXT", 'say "hi"', '"say \\"hi\\""'),
        ("CAA", '0 issue "letsencrypt.org"', '0 issue "letsencrypt.org"'),
        ("CAA", "0 iodef mailto:x@example.com", '0 iodef "mailto:x@example.com"'),
    ],
)
def test_content_valid(rtype, content, expected):
    assert dns.validate_content(rtype, content) == expected


@pytest.mark.parametrize(
    "rtype,content",
    [
        ("A", "999.0.0.1"),
        ("A", "2001:db8::1"),
        ("AAAA", "192.0.2.1"),
        ("CNAME", "bad host"),
        ("MX", "mail.example.com"),
        ("MX", "99999 mail.example.com"),
        ("SRV", "10 5 sip.example.com"),
        ("CAA", '0 unknown "x"'),
        ("TXT", "x" * 300),
        ("PTR", "whatever"),
        ("A", ""),
    ],
)
def test_content_invalid(rtype, content):
    with pytest.raises(dns.DNSValidationError):
        dns.validate_content(rtype, content)


# --- rrsets ------------------------------------------------------------------------


def test_make_rrset_normalizes():
    rrset = dns.make_rrset(ZONE, "www", "a", 3600, ["192.0.2.7"])
    assert rrset == dns.RRSet("www.example.com.", "A", 3600, ("192.0.2.7",))


def test_make_rrset_rejects_cname_apex():
    with pytest.raises(dns.DNSValidationError, match="apex"):
        dns.make_rrset(ZONE, "@", "CNAME", 3600, ["target.example.com"])


def test_make_rrset_rejects_multi_value_cname():
    with pytest.raises(dns.DNSValidationError):
        dns.make_rrset(ZONE, "www", "CNAME", 3600, ["a.example.com", "b.example.com"])


@pytest.mark.parametrize("ttl", [30, 0, 10_000_000])
def test_make_rrset_ttl_bounds(ttl):
    with pytest.raises(dns.DNSValidationError):
        dns.make_rrset(ZONE, "www", "A", ttl, ["192.0.2.1"])


def test_make_rrset_rejects_duplicates_and_empty():
    with pytest.raises(dns.DNSValidationError):
        dns.make_rrset(ZONE, "www", "A", 3600, ["192.0.2.1", "192.0.2.1"])
    with pytest.raises(dns.DNSValidationError):
        dns.make_rrset(ZONE, "www", "A", 3600, [])


def test_replace_patch_shape():
    rrset = dns.make_rrset(ZONE, "www", "A", 300, ["192.0.2.1", "192.0.2.2"])
    assert dns.replace_patch(rrset) == {
        "name": "www.example.com.",
        "type": "A",
        "ttl": 300,
        "changetype": "REPLACE",
        "records": [
            {"content": "192.0.2.1", "disabled": False},
            {"content": "192.0.2.2", "disabled": False},
        ],
    }


def test_delete_patch_shape():
    assert dns.delete_patch(ZONE, "www", "A") == {
        "name": "www.example.com.",
        "type": "A",
        "changetype": "DELETE",
        "records": [],
    }


def test_default_nameservers():
    assert dns.default_nameservers(ZONE, ["ns1.host.io"]) == ["ns1.host.io."]
    assert dns.default_nameservers(ZONE, []) == ["ns1.example.com.", "ns2.example.com."]


# --- PowerDNS client (mocked HTTP) -------------------------------------------------


def _client(handler):
    transport = httpx.MockTransport(handler)
    return dns.PowerDNSClient(
        "http://pdns:8053/api/v1", "secret-key", http=httpx.AsyncClient(transport=transport)
    )


async def test_list_zones_parses_and_authenticates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "secret-key"
        assert request.url.path == "/api/v1/servers/localhost/zones"
        return httpx.Response(
            200, json=[{"id": "a.com.", "name": "a.com.", "kind": "Native", "serial": 5}]
        )

    zones = await _client(handler).list_zones()
    assert zones == [dns.ZoneSummary("a.com.", "a.com.", "Native", 5)]


async def test_create_zone_conflict():
    def handler(request):
        return httpx.Response(409, json={"error": "exists"})

    with pytest.raises(ConflictError):
        await _client(handler).create_zone("a.com.", ["ns1.a.com."])


async def test_get_zone_not_found():
    def handler(request):
        return httpx.Response(404, json={"error": "no"})

    with pytest.raises(dns.ZoneNotFoundError):
        await _client(handler).get_zone("missing.com.")


async def test_patch_validation_error_surfaces():
    def handler(request):
        return httpx.Response(422, json={"error": "RRset is malformed"})

    with pytest.raises(dns.DNSValidationError, match="malformed"):
        await _client(handler).patch_rrsets("a.com.", [])


async def test_unreachable_maps_to_dns_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(dns.DNSError):
        await _client(handler).list_zones()
