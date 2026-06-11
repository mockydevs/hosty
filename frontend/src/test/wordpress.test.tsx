import { App } from "@/app";
import { jsonResponse, mockFetch } from "@/test/helpers";
/** WordPress tab: install wizard validation + installed status card. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

const USER = { id: 1, username: "admin", role: "admin" };
const TOKEN = { access_token: "t", token_type: "bearer", expires_in: 900 };

const SITE = {
  id: 1,
  domain: "example.com",
  site_user: "site-example-com-a1b2c3",
  doc_root: "/var/www/example.com/public_html",
  php_version: "8.3",
  status: "active",
  error_message: null,
  php_memory_limit: "256M",
  php_upload_max_filesize: "64M",
  wordpress: false,
  created_at: "2026-06-11T00:00:00Z",
};

function renderDetail(extra: Record<string, () => Response>) {
  mockFetch({
    "POST /api/auth/refresh": () => jsonResponse(TOKEN),
    "GET /api/auth/me": () => jsonResponse(USER),
    "GET /api/sites/1": () => jsonResponse(SITE),
    "GET /api/sites/1/ssl": () =>
      jsonResponse({
        domain: "example.com",
        status: "dns_unresolved",
        issuer: null,
        not_after: null,
        detail: "The domain does not resolve yet.",
      }),
    ...extra,
  });
  return render(
    <MemoryRouter initialEntries={["/sites/1"]}>
      <App />
    </MemoryRouter>,
  );
}

describe("WordPress tab", () => {
  it("shows the install wizard when WordPress is absent and validates fields", async () => {
    renderDetail({
      "GET /api/sites/1/wordpress": () =>
        jsonResponse({
          installed: false,
          version: null,
          update_available: null,
          plugin_count: null,
          theme_count: null,
        }),
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("tab", { name: "WordPress" }));
    await user.type(await screen.findByLabelText("Site title"), "My Blog");
    await user.type(screen.getByLabelText("Admin username"), "don");
    await user.type(screen.getByLabelText("Admin password"), "short");
    await user.type(screen.getByLabelText("Admin email"), "not-an-email");
    await user.click(screen.getByRole("button", { name: /install wordpress/i }));

    expect(await screen.findByText("At least 12 characters")).toBeInTheDocument();
    expect(screen.getByText("Enter a valid email")).toBeInTheDocument();
  });

  it("shows version, update badge and counts when installed", async () => {
    renderDetail({
      "GET /api/sites/1/wordpress": () =>
        jsonResponse({
          installed: true,
          version: "6.5.1",
          update_available: "6.6",
          plugin_count: 4,
          theme_count: 2,
        }),
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("tab", { name: "WordPress" }));

    expect(await screen.findByText("WordPress 6.5.1")).toBeInTheDocument();
    expect(screen.getByText("Update 6.6 available")).toBeInTheDocument();
    expect(screen.getByText("4 plugins · 2 themes")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /update core/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /admin login link/i })).toBeInTheDocument();
  });
});
