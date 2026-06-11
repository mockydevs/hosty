import { App } from "@/app";
import { jsonResponse, mockFetch } from "@/test/helpers";
/** Backups page: schedules, run-now operation, restore wizard guardrails. */
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
  databases: ["shop_db"],
  wordpress: false,
  php_version: "8.3",
  s3: false,
};

const SCHEDULE = {
  enabled: false,
  frequency: "daily",
  hour: 3,
  retention: 7,
  include_files: true,
  include_databases: true,
  s3_mirror: true,
  last_run_at: null,
};

const S3_CONFIG = {
  configured: false,
  source: null,
  endpoint: "",
  bucket: "",
  region: "",
  prefix: "hosty",
  access_key: "",
  has_secret: false,
};

const OPERATION = {
  id: 42,
  kind: "backup_site",
  site_id: 1,
  domain: "example.com",
  status: "succeeded",
  error: null,
  steps: [{ name: "files", label: "Archive site files", status: "done" }],
};

function renderBackups(extra: Record<string, (req: Request) => Response | Promise<Response>> = {}) {
  const fetchMock = mockFetch({
    "POST /api/auth/refresh": () => jsonResponse(TOKEN),
    "GET /api/auth/me": () => jsonResponse(USER),
    "GET /api/sites": () => jsonResponse([SITE]),
    "GET /api/backups/meta": () => jsonResponse({ s3_enabled: true, scheduler_enabled: true }),
    "GET /api/backups": () => jsonResponse([BACKUP]),
    "GET /api/sites/1/backup-schedule": () => jsonResponse(SCHEDULE),
    "GET /api/backups/s3-config": () => jsonResponse(S3_CONFIG),
    ...extra,
  });
  render(
    <MemoryRouter initialEntries={["/backups"]}>
      <App />
    </MemoryRouter>,
  );
  return fetchMock;
}

describe("BackupsPage", () => {
  it("lists sites, stored backups, and the S3 badge", async () => {
    renderBackups();
    expect(await screen.findByText("2026-06-11 03:15 UTC")).toBeInTheDocument();
    expect(screen.getByText(/s3 mirror on/i)).toBeInTheDocument();
    expect(screen.getByText("1.0 MiB")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /back up example\.com now/i })).toBeInTheDocument();
  });

  it("runs a backup now and shows live operation progress", async () => {
    renderBackups({
      "POST /api/sites/1/backups": () => jsonResponse({ operation_id: 42 }, 202),
      "GET /api/operations/42": () => jsonResponse(OPERATION),
    });
    await userEvent.click(await screen.findByRole("button", { name: /back up example\.com now/i }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/archive site files/i)).toBeInTheDocument();
  });

  it("restore wizard requires typing the domain", async () => {
    let restoreBody: unknown = null;
    renderBackups({
      "POST /api/sites/1/backups/20260611T031500Z/restore": async (req) => {
        restoreBody = await req.json();
        return jsonResponse({ operation_id: 43 }, 202);
      },
      "GET /api/operations/43": () => jsonResponse({ ...OPERATION, id: 43 }),
    });
    await userEvent.click(await screen.findByRole("button", { name: /restore 20260611T031500Z/i }));
    const dialog = screen.getByRole("dialog");
    const submit = within(dialog).getByRole("button", { name: /restore backup/i });
    expect(submit).toBeDisabled();

    await userEvent.click(within(dialog).getByLabelText(/files only/i));
    await userEvent.type(within(dialog).getByLabelText(/type the domain/i), "example.com");
    expect(submit).toBeEnabled();
    await userEvent.click(submit);

    expect(restoreBody).toEqual({ scope: "files", confirm_domain: "example.com" });
  });

  it("saves a schedule", async () => {
    let putBody: unknown = null;
    renderBackups({
      "PUT /api/sites/1/backup-schedule": async (req) => {
        putBody = await req.json();
        return jsonResponse({ ...SCHEDULE, enabled: true, frequency: "weekly" });
      },
    });
    await userEvent.click(await screen.findByRole("button", { name: /schedule/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByLabelText(/enable scheduled backups/i));
    await userEvent.selectOptions(within(dialog).getByLabelText(/frequency/i), "weekly");
    await userEvent.click(within(dialog).getByRole("button", { name: /save settings/i }));
    expect(await screen.findByText(/backup settings for example\.com saved/i)).toBeInTheDocument();
    expect(putBody).toEqual({
      enabled: true,
      frequency: "weekly",
      hour: 3,
      retention: 7,
      include_files: true,
      include_databases: true,
      s3_mirror: true,
    });
  });

  it("delete requires typing the backup id", async () => {
    renderBackups({
      "DELETE /api/sites/1/backups/20260611T031500Z": () => new Response(null, { status: 204 }),
    });
    await userEvent.click(await screen.findByRole("button", { name: /delete 20260611T031500Z/i }));
    const dialog = screen.getByRole("dialog");
    const submit = within(dialog).getByRole("button", { name: /delete backup/i });
    expect(submit).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText(/backup id/i), "20260611T031500Z");
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    expect(await screen.findByText(/backup deleted/i)).toBeInTheDocument();
  });
});

describe("S3ConfigDialog", () => {
  it("verifies and saves S3 credentials", async () => {
    let putBody: unknown = null;
    renderBackups({
      "PUT /api/backups/s3-config": async (req) => {
        putBody = await req.json();
        return jsonResponse({
          ...S3_CONFIG,
          configured: true,
          source: "db",
          endpoint: "https://s3.example.com",
          bucket: "my-backups",
          access_key: "AKIA123456",
          has_secret: true,
        });
      },
    });
    await userEvent.click(await screen.findByRole("button", { name: /s3 settings/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/endpoint url/i), "https://s3.example.com");
    await userEvent.type(within(dialog).getByLabelText(/bucket/i), "my-backups");
    await userEvent.type(within(dialog).getByLabelText(/access key id/i), "AKIA123456");
    await userEvent.type(within(dialog).getByLabelText(/secret access key/i), "shhh-very-secret");
    await userEvent.click(within(dialog).getByRole("button", { name: /verify & save/i }));

    expect(await screen.findByText(/s3 configuration verified and saved/i)).toBeInTheDocument();
    expect(putBody).toEqual({
      endpoint: "https://s3.example.com",
      bucket: "my-backups",
      region: "",
      prefix: "hosty",
      access_key: "AKIA123456",
      secret_key: "shhh-very-secret",
    });
  });

  it("surfaces credential errors from verification", async () => {
    renderBackups({
      "PUT /api/backups/s3-config": () =>
        jsonResponse(
          { error: { code: "backup_error", message: "S3 listing failed: InvalidAccessKeyId" } },
          502,
        ),
    });
    await userEvent.click(await screen.findByRole("button", { name: /s3 settings/i }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/endpoint url/i), "https://s3.example.com");
    await userEvent.type(within(dialog).getByLabelText(/bucket/i), "my-backups");
    await userEvent.type(within(dialog).getByLabelText(/access key id/i), "AKIA123456");
    await userEvent.type(within(dialog).getByLabelText(/secret access key/i), "bad");
    await userEvent.click(within(dialog).getByRole("button", { name: /verify & save/i }));
    expect(await within(dialog).findByText(/invalidaccesskeyid/i)).toBeInTheDocument();
  });
});
