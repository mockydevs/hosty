import { App } from "@/app";
import { normalizeDomain } from "@/pages/sites";
import { anonymousAuthRoutes, errorEnvelope, jsonResponse, mockFetch } from "@/test/helpers";
/** Sites: domain normalization matrix + create wizard validation + list rendering. */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

describe("normalizeDomain", () => {
  it.each([
    ["example.com", "example.com"],
    ["  EXAMPLE.COM ", "example.com"],
    ["example.com.", "example.com"],
    ["sub.deep.example.co.uk", "sub.deep.example.co.uk"],
    ["bücher.example", "xn--bcher-kva.example"],
    ["münchen.de", "xn--mnchen-3ya.de"],
  ])("accepts %s as %s", (raw, expected) => {
    expect(normalizeDomain(raw)).toBe(expected);
  });

  it.each([
    "",
    "example",
    "localhost",
    "exa mple.com",
    "http://example.com",
    "example.com/path",
    "example..com",
    "-bad.example",
    "bad-.example",
  ])("rejects %s", (raw) => {
    expect(normalizeDomain(raw)).toBeNull();
  });
});

const USER = { id: 1, username: "admin", role: "admin" };
const TOKEN = { access_token: "t", token_type: "bearer", expires_in: 900 };

const authedRoutes = {
  "POST /api/auth/refresh": () => jsonResponse(TOKEN),
  "GET /api/auth/me": () => jsonResponse(USER),
  "GET /api/auth/setup": () => jsonResponse({ setup_required: false }),
};

function renderSites(routes: Record<string, () => Response>, path = "/sites") {
  mockFetch({ ...anonymousAuthRoutes, ...authedRoutes, ...routes });
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}

const SITE = {
  id: 1,
  domain: "example.com",
  site_user: "site-example-com-abc123",
  doc_root: "/var/www/example.com/public_html",
  php_version: "8.3",
  status: "active",
  error_message: null,
  created_at: "2026-06-11T00:00:00Z",
};

describe("SitesPage", () => {
  it("lists sites with status and PHP badges", async () => {
    renderSites({ "GET /api/sites": () => jsonResponse([SITE]) });
    expect(await screen.findByText("example.com")).toBeInTheDocument();
    const table = within(screen.getByRole("table"));
    expect(table.getByText("active")).toBeInTheDocument();
    expect(table.getByText("PHP 8.3")).toBeInTheDocument();
  });

  it("shows the empty state when there are no sites", async () => {
    renderSites({ "GET /api/sites": () => jsonResponse([]) });
    expect(await screen.findByText("No sites yet")).toBeInTheDocument();
  });

  it("validates the domain in the create wizard without calling the API", async () => {
    renderSites({ "GET /api/sites": () => jsonResponse([]) });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /new site/i }));
    await user.type(screen.getByLabelText("Domain"), "not_a_domain");
    await user.click(screen.getByRole("button", { name: /create site/i }));

    expect(await screen.findByText("Enter a valid domain like example.com")).toBeInTheDocument();
  });

  it("previews the punycode form for unicode domains", async () => {
    renderSites({ "GET /api/sites": () => jsonResponse([]) });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /new site/i }));
    await user.type(screen.getByLabelText("Domain"), "bücher.example");

    expect(await screen.findByText("xn--bcher-kva.example")).toBeInTheDocument();
  });

  it("maps a server conflict onto the domain field", async () => {
    renderSites({
      "GET /api/sites": () => jsonResponse([SITE]),
      "POST /api/sites": () => errorEnvelope("A site for example.com already exists", 409),
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /new site/i }));
    await user.type(screen.getByLabelText("Domain"), "example.com");
    await user.click(screen.getByRole("button", { name: /create site/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "A site for example.com already exists",
    );
  });
});
