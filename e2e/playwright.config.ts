/**
 * E2E suite (Week 23) — runs against a LIVE Hosty VM, never against mocks.
 *   HOSTY_E2E_URL=http://<vm-ip>:8800 pnpm test
 * Serial: tests share one panel and build on each other's state.
 */
import { defineConfig } from "@playwright/test";

if (!process.env.HOSTY_E2E_URL) {
  throw new Error("Set HOSTY_E2E_URL to the panel URL of a running dev VM");
}

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  use: {
    baseURL: process.env.HOSTY_E2E_URL,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
