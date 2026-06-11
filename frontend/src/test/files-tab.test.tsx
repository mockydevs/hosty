import { App } from "@/app";
import { jsonResponse, mockFetch } from "@/test/helpers";
/** Files tab: opens a session and embeds the file manager iframe. */
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

describe("FilesTab", () => {
  it("opens a files session and embeds the iframe", async () => {
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
          detail: "x",
        }),
      "POST /api/sites/1/files-session": () =>
        jsonResponse({ url: "/files/?hosty_ticket=files-ticket:abc.123.sig" }),
    });
    render(
      <MemoryRouter initialEntries={["/sites/1"]}>
        <App />
      </MemoryRouter>,
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("tab", { name: "Files" }));
    await user.click(await screen.findByRole("button", { name: /open file manager/i }));

    const iframe = await screen.findByTitle("Files for example.com");
    expect(iframe).toHaveAttribute("src", "/files/?hosty_ticket=files-ticket:abc.123.sig");
    expect(screen.getByRole("button", { name: /open full screen/i })).toBeInTheDocument();
  });
});
