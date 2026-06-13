import { StackBackupsCard } from "@/pages/stack-detail";
import { jsonResponse, mockFetch } from "@/test/helpers";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

const STACK = {
  id: 7,
  name: "blog",
  blueprint_id: "wordpress",
  blueprint_version: 1,
  status: "ready",
  error_message: null,
  generation: 1,
  observed_generation: 1,
  created_at: "2026-06-13T00:00:00Z",
  services: [],
  volumes: [],
  endpoints: [],
  inputs: {},
};

const BACKUP = {
  domain: "stack--blog",
  backup_id: "20260613T001500Z",
  created_at: "2026-06-13T00:15:00Z",
  size_bytes: 2048,
  databases: ["wordpress"],
  wordpress: true,
  php_version: "8.3",
  s3: false,
};

describe("StackBackupsCard", () => {
  it("lists backups and starts an operation", async () => {
    const onOperation = vi.fn();
    mockFetch({
      "GET /api/stacks/7/backups": () => jsonResponse([BACKUP]),
      "POST /api/stacks/7/backups": () => jsonResponse({ operation_id: 41 }, 202),
    });
    render(
      <QueryClientProvider client={new QueryClient()}>
        <StackBackupsCard stack={STACK} onOperation={onOperation} />
      </QueryClientProvider>,
    );

    expect(await screen.findByText("20260613T001500Z")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /back up now/i }));
    expect(onOperation).toHaveBeenCalledWith(41);
  });
});
