import { App } from "@/app";
import { jsonResponse, mockFetch } from "@/test/helpers";
import { render, screen, within } from "@testing-library/react";
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

const BACKUP = {
  domain: "example.com",
  backup_id: "20260611T031500Z",
  created_at: "2026-06-11T03:15:00Z",
  size_bytes: 1024 * 1024,
  databases: ["example_db"],
  wordpress: true,
  php_version: "8.3",
  s3: true,
};

const SSL = {
  domain: "example.com",
  status: "active",
  issuer: "Let's Encrypt",
  not_after: "2026-09-11T00:00:00Z",
  detail: null,
};

function renderSiteDetail(
  extra: Record<string, (req: Request) => Response | Promise<Response>> = {},
) {
  mockFetch({
    "POST /api/auth/refresh": () => jsonResponse(TOKEN),
    "GET /api/auth/me": () => jsonResponse(USER),
    "GET /api/sites/1": () => jsonResponse(SITE),
    "GET /api/sites/1/ssl": () => jsonResponse(SSL),
    "GET /api/backups/meta": () => jsonResponse({ s3_enabled: true, scheduler_enabled: true }),
    "GET /api/sites/1/backups": () => jsonResponse([BACKUP]),
    ...extra,
  });
  render(
    <MemoryRouter initialEntries={["/sites/1"]}>
      <App />
    </MemoryRouter>,
  );
}

describe("SiteBackupsTab", () => {
  it("lists site backups and starts a backup from the site detail tab", async () => {
    renderSiteDetail({
      "POST /api/sites/1/backups": () => jsonResponse({ operation_id: 99 }, 202),
      "GET /api/operations/99": () =>
        jsonResponse({
          id: 99,
          kind: "backup_site",
          site_id: 1,
          domain: "example.com",
          status: "succeeded",
          error: null,
          created_at: "2026-06-11T03:20:00Z",
          finished_at: "2026-06-11T03:21:00Z",
          steps: [{ name: "files", label: "Archive site files", status: "done" }],
        }),
    });

    await userEvent.click(await screen.findByRole("tab", { name: /backups/i }));

    expect(await screen.findByText("2026-06-11 03:15 UTC")).toBeInTheDocument();
    expect(screen.getByText("1.0 MiB")).toBeInTheDocument();
    expect(screen.getAllByText("WordPress")).toHaveLength(2);
    expect(screen.getByText("S3 configured")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /back up now/i }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/archive site files/i)).toBeInTheDocument();
  });
});
