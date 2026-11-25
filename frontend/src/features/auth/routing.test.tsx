import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SplashPage } from "../../pages/SplashPage";
import { fakeSession, officer, passenger, renderAt } from "../../test/utils";
import { RequireRole } from "./RequireRole";

describe("RequireRole", () => {
  it("sends signed-out visitors to sign in", () => {
    renderAt(<RequireRole role="officer"><p>secret</p></RequireRole>, { path: "/officer", route: "/officer" });
    expect(screen.getByTestId("where")).toHaveTextContent("/login");
  });

  it("sends other roles to their own home", () => {
    const session = fakeSession({ status: "signedIn", user: passenger, token: "t" });
    renderAt(<RequireRole role="officer"><p>secret</p></RequireRole>, { path: "/officer", route: "/officer", session });
    expect(screen.getByTestId("where")).toHaveTextContent("/passenger");
  });

  it("lets the right role in", () => {
    const session = fakeSession({ status: "signedIn", user: officer, token: "t" });
    renderAt(<RequireRole role="officer"><p>secret</p></RequireRole>, { path: "/officer", route: "/officer", session });
    expect(screen.getByText("secret")).toBeInTheDocument();
  });
});

describe("SplashPage", () => {
  afterEach(() => vi.useRealTimers());

  it("continues to sign in when the animation ends", () => {
    vi.useFakeTimers();
    renderAt(<SplashPage durationMs={6000} />);
    expect(screen.getByText("Loading routes…")).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(6000));
    expect(screen.getByTestId("where")).toHaveTextContent("/login");
  });

  it("continues straight away on click, to the signed-in role's home", () => {
    const session = fakeSession({ status: "signedIn", user: officer, token: "t" });
    renderAt(<SplashPage />, { session });
    fireEvent.click(screen.getByRole("link", { name: /continue to your dashboard/i }));
    expect(screen.getByTestId("where")).toHaveTextContent("/officer");
  });
});
