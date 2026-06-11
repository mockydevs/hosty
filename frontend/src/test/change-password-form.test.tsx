import { ChangePasswordForm } from "@/pages/settings";
import { errorEnvelope, mockFetch } from "@/test/helpers";
/** Form pattern test: Zod validation + server errors mapped to fields. */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

async function fill(values: { current?: string; next?: string; confirm?: string }) {
  const user = userEvent.setup();
  if (values.current) await user.type(screen.getByLabelText(/current password/i), values.current);
  if (values.next) await user.type(screen.getByLabelText(/^new password$/i), values.next);
  if (values.confirm)
    await user.type(screen.getByLabelText(/confirm new password/i), values.confirm);
  await user.click(screen.getByRole("button", { name: /change password/i }));
}

describe("ChangePasswordForm", () => {
  it("validates client-side without calling the API", async () => {
    const fetchMock = mockFetch({});
    render(<ChangePasswordForm />);

    await fill({ current: "old-password", next: "short", confirm: "short" });

    expect(await screen.findByText("At least 12 characters")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects mismatched confirmation", async () => {
    mockFetch({});
    render(<ChangePasswordForm />);

    await fill({
      current: "old-password",
      next: "a-long-enough-password",
      confirm: "different-password",
    });

    expect(await screen.findByText("Passwords do not match")).toBeInTheDocument();
  });

  it("maps a server error onto the current-password field", async () => {
    mockFetch({
      "POST /api/auth/change-password": () => errorEnvelope("Current password is incorrect", 401),
    });
    render(<ChangePasswordForm />);

    await fill({
      current: "wrong-password",
      next: "a-long-enough-password",
      confirm: "a-long-enough-password",
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("Current password is incorrect");
  });

  it("calls onChanged after a successful change", async () => {
    mockFetch({ "POST /api/auth/change-password": () => new Response(null, { status: 204 }) });
    const onChanged = vi.fn();
    render(<ChangePasswordForm onChanged={onChanged} />);

    await fill({
      current: "old-password",
      next: "a-long-enough-password",
      confirm: "a-long-enough-password",
    });

    await vi.waitFor(() => expect(onChanged).toHaveBeenCalledOnce());
  });
});
