import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import { SignupPage } from "../../pages/AuthPages";
import { fakeSession, passenger, renderAt } from "../../test/utils";

const setup = (register = vi.fn()) => {
  renderAt(<SignupPage />, { path: "/signup", route: "/signup", session: fakeSession({ register }) });
  return { user: userEvent.setup(), register };
};

async function fill(user: ReturnType<typeof userEvent.setup>, pw2 = "Secret123") {
  await user.type(screen.getByLabelText("Your name"), "Asha");
  await user.type(screen.getByLabelText("Email address"), "asha@example.com");
  await user.type(screen.getByLabelText("Create password"), "Secret123");
  await user.type(screen.getByLabelText("Re-enter password"), pw2);
}

describe("SignupForm", () => {
  it("validates name and email only after submitting", async () => {
    const { user, register } = setup();
    expect(screen.queryByText("Enter your name.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByText("Enter your name.")).toBeInTheDocument();
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    expect(screen.getByText("Re-enter your password.")).toBeInTheDocument();
    expect(register).not.toHaveBeenCalled();
  });

  it("shows live password length and match status", async () => {
    const { user } = setup();
    const pw = screen.getByLabelText("Create password");
    await user.type(pw, "short");
    expect(pw).not.toHaveAttribute("aria-invalid", "true");
    await user.type(pw, "enough");
    await user.type(screen.getByLabelText("Re-enter password"), "nope");
    expect(screen.getByText("Passwords don’t match")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Re-enter password"));
    await user.type(screen.getByLabelText("Re-enter password"), "shortenough");
    expect(screen.getByText("Passwords match")).toBeInTheDocument();
  });

  it("one toggle shows both passwords", async () => {
    const { user } = setup();
    await user.click(screen.getByRole("button", { name: "Show passwords" }));
    expect(screen.getByLabelText("Create password")).toHaveAttribute("type", "text");
    expect(screen.getByLabelText("Re-enter password")).toHaveAttribute("type", "text");
  });

  it("creates the account and shows the success view", async () => {
    const register = vi.fn().mockResolvedValue({ ...passenger, email: "asha@example.com" });
    const { user } = setup(register);
    await fill(user);
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(register).toHaveBeenCalledWith("Asha", "asha@example.com", "Secret123");
    expect(await screen.findByText("Account created")).toBeInTheDocument();
    expect(screen.getByText("You can now sign in with asha@example.com.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to sign in" })).toHaveAttribute("href", "/login");
  });

  it("explains an email that is already registered", async () => {
    const register = vi.fn().mockRejectedValue(new ApiError("conflict", 409, "Email already registered"));
    const { user } = setup(register);
    await fill(user);
    await user.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("already exists");
  });
});
