import { App } from "@/app";
import { jsonResponse, mockFetch } from "@/test/helpers";
/** Databases page: list with orphans, create flow shows credentials once. */
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

const DB_ENTRY = {
  database: {
    id: 7,
    site_id: 1,
    name: "shop_db",
    db_user: "shop_db",
    purpose: "custom",
    created_at: "2026-06-11T00:00:00Z",
  },
  site_domain: "example.com",
  orphan_name: null,
  missing: false,
};

const ORPHAN_ENTRY = { database: null, site_domain: null, orphan_name: "stray_db", missing: false };

function renderDatabases(extra: Record<string, (req: Request) => Response | Promise<Response>>) {
  mockFetch({
    "POST /api/auth/refresh": () => jsonResponse(TOKEN),
    "GET /api/auth/me": () => jsonResponse(USER),
    "GET /api/sites": () => jsonResponse([SITE]),
    ...extra,
  });
  return render(
    <MemoryRouter initialEntries={["/databases"]}>
      <App />
    </MemoryRouter>,
  );
}

describe("DatabasesPage", () => {
  it("lists databases and flags orphans", async () => {
    renderDatabases({ "GET /api/databases": () => jsonResponse([DB_ENTRY, ORPHAN_ENTRY]) });

    expect(await screen.findByText("shop_db")).toBeInTheDocument();
    expect(screen.getByText("stray_db")).toBeInTheDocument();
    expect(screen.getByText("orphan — not managed by Hosty")).toBeInTheDocument();
    expect(screen.getByText("custom")).toBeInTheDocument();
  });

  it("creates a database and shows credentials exactly once", async () => {
    renderDatabases({
      "GET /api/databases": () => jsonResponse([]),
      "POST /api/databases/sites/1": () =>
        jsonResponse(
          {
            database: DB_ENTRY.database,
            password: "deadbeef".repeat(6),
          },
          201,
        ),
    });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /new database/i }));
    await user.type(await screen.findByLabelText("Database name"), "shop_db");
    await user.click(screen.getByRole("button", { name: /create database/i }));

    const dialog = await screen.findByText("Database credentials");
    expect(dialog).toBeInTheDocument();
    expect(screen.getByText("deadbeef".repeat(6))).toBeInTheDocument();
    expect(screen.getByText(/shown only this once/i)).toBeInTheDocument();
  });

  it("rejects invalid database names client-side", async () => {
    renderDatabases({ "GET /api/databases": () => jsonResponse([]) });
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: /new database/i }));
    await user.type(await screen.findByLabelText("Database name"), "Bad-Name");
    await user.click(screen.getByRole("button", { name: /create database/i }));

    expect(await screen.findByText(/lowercase letters, digits and _/i)).toBeInTheDocument();
  });
});
