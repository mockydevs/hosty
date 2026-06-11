import { App } from "@/app";
import { setAccessToken } from "@/lib/api/client";
import { anonymousAuthRoutes, errorEnvelope, jsonResponse, mockFetch } from "@/test/helpers";
/**
 * Auth flow: anonymous user is redirected to /login, logs in, lands on the
 * dashboard; wrong password shows the server error.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it } from "vitest";

function renderApp(route = "/") {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <App />
    </MemoryRouter>,
  );
}

const USER = { id: 1, username: "admin", role: "admin" };
const TOKEN = { access_token: "test-token", token_type: "bearer", expires_in: 900 };

afterEach(() => {
  setAccessToken(null);
});

describe("auth flow", () => {
  it("redirects anonymous visitors to the login page", async () => {
    mockFetch({ ...anonymousAuthRoutes });
    renderApp("/");
    expect(await screen.findByRole("button", { name: /log in/i })).toBeInTheDocument();
  });

  it("logs in and shows the dashboard", async () => {
    mockFetch({
      ...anonymousAuthRoutes,
      "POST /api/auth/login": () => jsonResponse(TOKEN),
      "GET /api/auth/me": (req) =>
        req.headers.get("Authorization") === `Bearer ${TOKEN.access_token}`
          ? jsonResponse(USER)
          : errorEnvelope("Not authenticated", 401),
      "GET /api/system/stats": () =>
        jsonResponse({
          cpu_percent: 12.5,
          load_avg: [0.1, 0.2, 0.3],
          memory_total: 8_000_000_000,
          memory_used: 4_000_000_000,
          memory_percent: 50,
          disk_total: 100_000_000_000,
          disk_used: 25_000_000_000,
          disk_percent: 25,
          uptime_seconds: 3600,
        }),
      "GET /api/system/services": () =>
        jsonResponse([
          {
            unit: "caddy",
            available: true,
            active_state: "active",
            sub_state: "running",
            enabled: "enabled",
          },
        ]),
    });
    renderApp("/");

    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/username/i), "admin");
    await user.type(screen.getByLabelText(/password/i), "correct horse battery");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    expect(await screen.findByRole("heading", { name: "Dashboard" })).toBeInTheDocument();
    expect(await screen.findByText("Caddy")).toBeInTheDocument();
    expect(screen.getByText("Running")).toBeInTheDocument();
  });

  it("shows the server error on wrong credentials", async () => {
    mockFetch({
      ...anonymousAuthRoutes,
      "POST /api/auth/login": () => errorEnvelope("Invalid username or password", 401),
    });
    renderApp("/login");

    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/username/i), "admin");
    await user.type(screen.getByLabelText(/password/i), "wrong-password");
    await user.click(screen.getByRole("button", { name: /log in/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid username or password");
  });

  it("shows the first-boot setup form when no users exist", async () => {
    mockFetch({
      "POST /api/auth/refresh": () => errorEnvelope("Not authenticated", 401),
      "GET /api/auth/setup": () => jsonResponse({ setup_required: true }),
    });
    renderApp("/login");
    expect(
      await screen.findByRole("button", { name: /create admin account/i }),
    ).toBeInTheDocument();
  });
});
