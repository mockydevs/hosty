from __future__ import annotations

import httpx
import pytest

from app.services import cloudflare as cf

RRSETS = [
    {"name": "example.com.", "type": "SOA", "ttl": 3600, "records": ["ns1. host. 1 2 3 4 5"]},
    {
        "name": "example.com.",
        "type": "NS",
        "ttl": 3600,
        "records": ["ns1.example.com.", "ns2.example.com."],
    },
    {"name": "example.com.", "type": "A", "ttl": 3600, "records": ["192.0.2.1"]},
    {"name": "www.example.com.", "type": "CNAME", "ttl": 3600, "records": ["example.com."]},
    {"name": "example.com.", "type": "MX", "ttl": 3600, "records": ["10 mail.example.com."]},
    {"name": "example.com.", "type": "TXT", "ttl": 3600, "records": ['"v=spf1 mx ~all"']},
]


def test_desired_records_mapping():
    desired, errors = cf.desired_records("example.com", RRSETS)
    assert errors == []
    by_key = {(d["type"], d["name"]): d for d in desired}
    assert ("SOA", "example.com") not in by_key  # never pushed
    assert ("NS", "example.com") not in by_key  # apex NS is Cloudflare's
    assert by_key[("A", "example.com")]["content"] == "192.0.2.1"
    assert by_key[("A", "example.com")]["proxied"] is False
    assert by_key[("CNAME", "www.example.com")]["content"] == "example.com"
    mx = by_key[("MX", "example.com")]
    assert mx["content"] == "mail.example.com"
    assert mx["priority"] == 10
    assert by_key[("TXT", "example.com")]["content"] == "v=spf1 mx ~all"


def test_desired_records_srv_caa_and_errors():
    rrsets = [
        {
            "name": "_sip._tcp.example.com.",
            "type": "SRV",
            "ttl": 300,
            "records": ["10 5 5060 sip.example.com."],
        },
        {
            "name": "example.com.",
            "type": "CAA",
            "ttl": 300,
            "records": ['0 issue "letsencrypt.org"'],
        },
        {"name": "plain.example.com.", "type": "SRV", "ttl": 300, "records": ["10 5 5060 x.com."]},
    ]
    desired, errors = cf.desired_records("example.com", rrsets)
    srv = next(d for d in desired if d["type"] == "SRV")
    assert srv["data"] == {
        "service": "_sip",
        "proto": "_tcp",
        "name": "example.com",
        "priority": 10,
        "weight": 5,
        "port": 5060,
        "target": "sip.example.com",
    }
    caa = next(d for d in desired if d["type"] == "CAA")
    assert caa["data"] == {"flags": 0, "tag": "issue", "value": "letsencrypt.org"}
    assert len(errors) == 1
    assert "plain.example.com" in errors[0]


class FakeCF:
    """In-memory stand-in for CloudflareClient (used here and by the API tests)."""

    def __init__(self, zone=None, existing=None):
        self.zone = zone
        self.existing = existing or []
        self.created: list[dict] = []
        self.updated: list[tuple[str, dict]] = []

    async def find_zone(self, name):
        return self.zone

    async def list_records(self, zone_id):
        return self.existing

    async def create_record(self, zone_id, payload):
        self.created.append(payload)

    async def update_record(self, zone_id, record_id, payload):
        self.updated.append((record_id, payload))


async def test_push_zone_missing_in_cloudflare():
    with pytest.raises(cf.ZoneNotInCloudflareError):
        await cf.push_zone(FakeCF(zone=None), "example.com.", [])


async def test_push_create_skip_update():
    existing = [
        {"id": "1", "type": "A", "name": "example.com", "content": "192.0.2.1"},  # identical
        {"id": "2", "type": "CNAME", "name": "www.example.com", "content": "old.example.com"},
    ]
    fake = FakeCF(zone={"id": "z1"}, existing=existing)
    result = await cf.push_zone(fake, "example.com.", RRSETS)
    assert result.skipped == 1  # A unchanged
    assert result.updated == 1  # CNAME changed in place
    assert fake.updated[0][0] == "2"
    assert result.created == 2  # MX + TXT were missing
    assert result.errors == []


async def test_push_txt_quote_insensitive_and_mx_priority():
    existing = [
        {"id": "1", "type": "TXT", "name": "example.com", "content": "v=spf1 mx ~all"},
        {
            "id": "2",
            "type": "MX",
            "name": "example.com",
            "content": "mail.example.com",
            "priority": 20,  # differs -> update
        },
    ]
    fake = FakeCF(zone={"id": "z"}, existing=existing)
    result = await cf.push_zone(fake, "example.com.", [RRSETS[4], RRSETS[5]])
    assert result.skipped == 1  # TXT matches despite quoting differences
    assert result.updated == 1  # MX priority corrected
    assert result.created == 0


async def test_push_multi_value_rrset_creates_missing():
    rrsets = [
        {"name": "example.com.", "type": "A", "ttl": 300, "records": ["192.0.2.1", "192.0.2.2"]}
    ]
    existing = [{"id": "1", "type": "A", "name": "example.com", "content": "192.0.2.1"}]
    fake = FakeCF(zone={"id": "z"}, existing=existing)
    result = await cf.push_zone(fake, "example.com.", rrsets)
    # one identical (skip) + one missing (create) — never update a multi-value set in place
    assert result.skipped == 1
    assert result.created == 1
    assert result.updated == 0


# --- HTTP client (mocked) ----------------------------------------------------------


def _client(handler):
    return cf.CloudflareClient(
        "token-x",
        base_url="https://cf.test/client/v4",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_find_zone_sends_bearer_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token-x"
        assert request.url.path == "/client/v4/zones"
        assert request.url.params["name"] == "example.com"
        return httpx.Response(
            200,
            json={"success": True, "errors": [], "result": [{"id": "abc", "name": "example.com"}]},
        )

    zone = await _client(handler).find_zone("example.com")
    assert zone == {"id": "abc", "name": "example.com"}


async def test_find_zone_empty_result():
    def handler(request):
        return httpx.Response(200, json={"success": True, "errors": [], "result": []})

    assert await _client(handler).find_zone("nope.com") is None


async def test_api_error_message_surfaces():
    def handler(request):
        return httpx.Response(
            403, json={"success": False, "errors": [{"message": "Invalid API token"}]}
        )

    with pytest.raises(cf.CloudflareError, match="Invalid API token"):
        await _client(handler).find_zone("example.com")


async def test_unreachable_maps_to_cloudflare_error():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(cf.CloudflareError, match="unreachable"):
        await _client(handler).find_zone("example.com")


async def test_list_records_paginates():
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(
            200,
            json={
                "success": True,
                "errors": [],
                "result": [{"id": f"r{page}"}],
                "result_info": {"total_pages": 2},
            },
        )

    records = await _client(handler).list_records("z1")
    assert [r["id"] for r in records] == ["r1", "r2"]
    assert pages == [1, 2]


# --- pull: Cloudflare records -> panel rrsets ---------------------------------------


def test_pulled_rrsets_maps_types_and_skips_apex_ns():
    records = [
        {"type": "A", "name": "mail.example.com", "content": "84.46.251.171", "ttl": 1},
        {"type": "AAAA", "name": "mail.example.com", "content": "2a02:c207::1", "ttl": 1},
        {
            "type": "CNAME",
            "name": "autoconfig.example.com",
            "content": "mail.example.com",
            "ttl": 300,
        },
        {
            "type": "MX",
            "name": "example.com",
            "content": "smtp.google.com",
            "priority": 1,
            "ttl": 1,
        },
        {"type": "TXT", "name": "example.com", "content": "v=spf1 ~all", "ttl": 3600},
        {
            "type": "SRV",
            "name": "_imaps._tcp.example.com",
            "content": "10 993 mail.example.com",
            "ttl": 1,
            "data": {"priority": 10, "weight": 0, "port": 993, "target": "mail.example.com"},
        },
        {"type": "NS", "name": "example.com", "content": "audrey.ns.cloudflare.com", "ttl": 1},
        {"type": "HTTPS", "name": "example.com", "content": "1 . alpn=h2", "ttl": 1},
    ]
    out, errors = cf.pulled_rrsets("example.com.", records, 3600)
    by_key = {(name, rtype): (ttl, contents) for name, rtype, ttl, contents in out}
    assert by_key[("mail.example.com.", "A")] == (3600, ["84.46.251.171"])  # ttl=1 -> default
    assert by_key[("autoconfig.example.com.", "CNAME")] == (300, ["mail.example.com."])
    assert by_key[("example.com.", "MX")][1] == ["1 smtp.google.com."]
    assert by_key[("example.com.", "TXT")][1] == ["v=spf1 ~all"]
    assert by_key[("_imaps._tcp.example.com.", "SRV")][1] == ["10 0 993 mail.example.com."]
    # Apex NS (Cloudflare's own nameservers) is never imported.
    assert ("example.com.", "NS") not in by_key
    assert errors == ["HTTPS example.com: unsupported type"]


def test_pulled_rrsets_groups_multi_value_sets():
    records = [
        {"type": "A", "name": "example.com", "content": "192.0.2.1", "ttl": 1},
        {"type": "A", "name": "example.com", "content": "192.0.2.2", "ttl": 1},
        {"type": "MX", "name": "example.com", "content": "mx1.example.com", "priority": 10},
        {"type": "MX", "name": "example.com", "content": "mx2.example.com", "priority": 20},
    ]
    out, errors = cf.pulled_rrsets("example.com.", records, 3600)
    by_key = {(name, rtype): contents for name, rtype, _, contents in out}
    assert by_key[("example.com.", "A")] == ["192.0.2.1", "192.0.2.2"]
    assert by_key[("example.com.", "MX")] == ["10 mx1.example.com.", "20 mx2.example.com."]
    assert errors == []
