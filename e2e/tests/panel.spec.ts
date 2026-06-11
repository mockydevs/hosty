/**
 * Happy-path E2E against a fresh dev VM (Week 23):
 * first-boot setup → login → create site → install WordPress → backup.
 * Run order matters (serial); a fresh VM database is assumed.
 */
import { expect, test } from "@playwright/test";

const ADMIN = { username: "e2e-admin", password: "correct-horse-battery-staple" };
const DOMAIN = "e2e.hosty.test";

test.describe.serial("Hosty happy path", () => {
  test("first-boot setup creates the admin and logs in", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText(/create the admin account/i)).toBeVisible();
    await page.getByLabel("Admin username").fill(ADMIN.username);
    await page.getByLabel("Password", { exact: true }).fill(ADMIN.password);
    await page.getByLabel("Confirm password").fill(ADMIN.password);
    await page.getByRole("button", { name: /create admin account/i }).click();
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
  });

  test("create a site and wait for it to go active", async ({ page }) => {
    await login(page);
    await page.goto("/sites");
    await page.getByRole("button", { name: /new site/i }).click();
    await page.getByLabel("Domain").fill(DOMAIN);
    await page.getByRole("button", { name: /create site/i }).click();
    await expect(page.getByText("active", { exact: true })).toBeVisible({ timeout: 90_000 });
  });

  test("install WordPress on the site", async ({ page }) => {
    await login(page);
    await page.goto("/sites");
    await page.getByRole("link", { name: DOMAIN }).click();
    await page.getByRole("tab", { name: "WordPress" }).click();
    await page.getByLabel("Site title").fill("E2E Blog");
    await page.getByLabel("Admin username").fill(ADMIN.username);
    await page.getByLabel("Admin password").fill(ADMIN.password);
    await page.getByLabel("Admin email").fill("e2e@example.com");
    await page.getByRole("button", { name: /install wordpress/i }).click();
    await expect(page.getByText(/WordPress 6/)).toBeVisible({ timeout: 300_000 });
  });

  test("run a backup and see it listed", async ({ page }) => {
    await login(page);
    await page.goto("/backups");
    await page.getByRole("button", { name: /run now/i }).first().click();
    await expect(page.getByText(/succeeded|completed/i)).toBeVisible({ timeout: 180_000 });
  });
});

async function login(page) {
  await page.goto("/login");
  if (page.url().includes("/login")) {
    await page.getByLabel(/username/i).fill(ADMIN.username);
    await page.getByLabel(/password/i).fill(ADMIN.password);
    await page.getByRole("button", { name: /log in/i }).click();
    await page.waitForURL((url) => !url.pathname.includes("/login"));
  }
}
