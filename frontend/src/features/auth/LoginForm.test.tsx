import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api/client";
import { fakeSession, officer, renderAt } from "../../test/utils";
import { LoginForm } from "./LoginForm";

const setup = (login = vi.fn()) => {
  const session = fakeSession({ login });
  renderAt(<LoginForm variant="desktop" />, { path: "/login", route: "/login", session });
  return { user: userEvent.setup(), login };
};

describe("LoginForm", () => {
  it("shows no errors until the first submit, then marks the bad fields", async () => {
    const { user, login } = setup();
    expect(screen.queryByText("Enter a valid email address.")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Email address"), "not-an-email");
    expect(screen.queryByText("Enter a valid email address.")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByText("Enter a valid email address.")).toBeInTheDocument();
    expect(screen.getByText("Enter your password.")).toBeInTheDocument();
    expect(screen.getByLabelText("Email address")).toHaveAttribute("aria-invalid", "true");
    expect(login).not.toHaveBeenCalled();
  });

  it("signs in and goes to the role's home", async () => {
    let finish: (u: typeof officer) => void = () => {};
    const login = vi.fn(() => new Promise<typeof officer>((r) => (finish = r)));
    const { user } = setup(login);
    await user.type(screen.getByLabelText("Email address"), "officer@example.com");
    await user.type(screen.getByLabelText("Password"), "Test@123");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(login).toHaveBeenCalledWith("officer@example.com", "Test@123");
    expect(screen.getByRole("button", { name: "Signing in…" })).toBeDisabled();
    finish(officer);
    expect(await screen.findByTestId("where")).toHaveTextContent("/officer");
  });

  it("explains wrong credentials", async () => {
    const login = vi.fn().mockRejectedValue(new ApiError("unauthorized", 401, "Invalid credentials"));
    const { user } = setup(login);
    await user.type(screen.getByLabelText("Email address"), "officer@example.com");
    await user.type(screen.getByLabelText("Password"), "wrong");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("don't match an account");
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("toggles password visibility", async () => {
    const { user } = setup();
    const field = screen.getByLabelText("Password");
    expect(field).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Show password" }));
    expect(field).toHaveAttribute("type", "text");
    expect(screen.getByRole("button", { name: "Hide password" })).toHaveAttribute("aria-pressed", "true");
  });
});
