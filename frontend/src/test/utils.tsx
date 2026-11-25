import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { vi } from "vitest";

import { SessionContext, type Session } from "../features/auth/session";

export function fakeSession(overrides: Partial<Session> = {}): Session {
  return {
    status: "signedOut",
    user: null,
    token: null,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    ...overrides,
  };
}

/** Shows the current path so tests can assert where navigation went. */
function Where() {
  const location = useLocation();
  return <div data-testid="where">{location.pathname}</div>;
}

export function renderAt(
  ui: ReactElement,
  { path = "/", route = "/", session = fakeSession(), state }: { path?: string; route?: string; session?: Session; state?: unknown } = {},
) {
  return render(
    <SessionContext.Provider value={session}>
      <MemoryRouter initialEntries={[{ pathname: route, state }]}>
        <Routes>
          <Route path={path} element={ui} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </SessionContext.Provider>,
  );
}

export const officer = { id: "1", email: "officer@example.com", role: "officer" as const, display_name: "Officer" };
export const passenger = { id: "2", email: "rider@example.com", role: "passenger" as const, display_name: "Rider" };
