import { App } from "@/app";
import { errorEnvelope, jsonResponse, mockFetch } from "@/test/helpers";
/** DNS pages: zone list, record editor guardrails, Cloudflare push. */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

const USER = { id: 1, username: "admin", role: "admin" };
const TOKEN = { access_token: "t", token_type: "bearer", expires_in: 900 };

const META = {
  enabled: true,
  server_ip: "203.0.113.7",
  default_ttl: 3600,
  cloudflare_enabled: true,
};

const ZONES = [{ id: "example.com.", name: "example.com.", kind: "Native", serial: 3 }];

const ZONE_DETAIL = {
  id: "example.com.",
  name: "example.com.",
  serial: 3,
  rrsets: [
    {
      name: "example.com.",
      type: "SOA",
      ttl: 3600,
      records: ["ns1.example.com. hostmaster.example.com. 3 10800 3600 604800 3600"],
    },
    {
      name: "example.com.",
      type: "NS",
      ttl: 3600,
      records: ["ns1.example.com.", "ns2.example.com."],
    },
    { name: "www.example.com.", type: "A", ttl: 300, records: ["192.0.2.10"] },
  ],
};

function renderDns(
  path: string,
  extra: Record<string, (req: Request) => Response | Promise<Response>> = {},
) {
  const fetchMock = mockFetch({
    "POST /api/auth/refresh": () => jsonResponse(TOKEN),
    "GET /api/auth/me": () => jsonResponse(USER),
    "GET /api/dns/meta": () => jsonResponse(META),
    "GET /api/dns/zones": () => jsonResponse(ZONES),
    "GET /api/dns/zones/example.com.": () => jsonResponse(ZONE_DETAIL),
    ...extra,
  });
  render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
  return fetchMock;
}

describe("DnsPage", () => {
  it("lists zones with links to the editor", async () => {
    renderDns("/dns");
    const link = await screen.findByRole("link", { name: /example\.com/ });
    expect(link).toHaveAttribute("href", "/dns/example.com.");
    expect(screen.getByRole("button", { name: /new zone/i })).toBeInTheDocument();
  });

  it("validates the create-zone form client-side", async () => {
    renderDns("/dns");
    await userEvent.click(await screen.findByRole("button", { name: /new zone/i }));
    await userEvent.click(screen.getByRole("button", { name: /create zone/i }));
    expect(await screen.findByText(/zone name is required/i)).toBeInTheDocument();
  });
});

describe("DnsZonePage", () => {
  it("renders records and protects SOA and apex NS", async () => {
    renderDns("/dns/example.com.");
    expect(await screen.findByText("192.0.2.10")).toBeInTheDocument();

    // SOA: no edit/delete actions at all
    expect(
      screen.queryByRole("button", { name: /edit SOA example\.com\./i }),
    ).not.toBeInTheDocument();
    // apex NS: editable but NOT deletable
    expect(screen.getByRole("button", { name: /edit NS example\.com\./i })).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /delete NS example\.com\./i }),
    ).not.toBeInTheDocument();
    // normal record: both actions
    expect(
      screen.getByRole("button", { name: /delete A www\.example\.com\./i }),
    ).toBeInTheDocument();
  });

  it("surfaces server-side validation errors in the record dialog", async () => {
    renderDns("/dns/example.com.", {
      "PUT /api/dns/zones/example.com./records": () =>
        errorEnvelope("Invalid IPv4 address: 'nope'", 422),
    });
    await userEvent.click(await screen.findByRole("button", { name: /add record/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/name/i), "www");
    await userEvent.type(within(dialog).getByLabelText(/values/i), "nope");
    await userEvent.click(within(dialog).getByRole("button", { name: /add record/i }));
    expect(await within(dialog).findByText(/invalid ipv4 address/i)).toBeInTheDocument();
  });

  it("pushes all records to Cloudflare with one click", async () => {
    const fetchMock = renderDns("/dns/example.com.", {
      "POST /api/dns/zones/example.com./push/cloudflare": () =>
        jsonResponse({ zone: "example.com.", created: 2, updated: 1, skipped: 3, errors: [] }),
    });
    await userEvent.click(await screen.findByRole("button", { name: /push to cloudflare/i }));
    expect(
      await screen.findByText(/cloudflare: 2 created, 1 updated, 3 unchanged/i),
    ).toBeInTheDocument();
    const calls = (fetchMock as unknown as { mock: { calls: [Request][] } }).mock.calls;
    expect(
      calls.some(
        ([req]) => new URL(req.url).pathname === "/api/dns/zones/example.com./push/cloudflare",
      ),
    ).toBe(true);
  });

  it("hides the Cloudflare button when no token is configured", async () => {
    renderDns("/dns/example.com.", {
      "GET /api/dns/meta": () => jsonResponse({ ...META, cloudflare_enabled: false }),
    });
    await screen.findByText("192.0.2.10");
    expect(screen.queryByRole("button", { name: /push to cloudflare/i })).not.toBeInTheDocument();
  });
});
