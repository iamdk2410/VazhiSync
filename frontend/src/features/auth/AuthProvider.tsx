import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import { authApi, type User } from "../../api/auth";
import { ApiError } from "../../api/client";
import { SessionContext, TOKEN_KEY, type AuthStatus, type Session } from "./session";

function storedToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null; // storage blocked: just don't remember the session
  }
}

function storeToken(token: string | null): void {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage blocked */
  }
}

/** Holds the signed-in user. A remembered token is checked with the server (`/auth/me`) on
 *  start-up; roles always come from the server, never from what the browser remembers. */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => storedToken());
  const [user, setUser] = useState<User | null>(null);
  const [status, setStatus] = useState<AuthStatus>(() => (storedToken() ? "loading" : "signedOut"));

  useEffect(() => {
    if (!token || user) return;
    const controller = new AbortController();
    authApi
      .me(token, controller.signal)
      .then((me) => {
        setUser(me);
        setStatus("signedIn");
      })
      .catch((err: unknown) => {
        if (err instanceof DOMException && err.name === "AbortError") return;
        if (err instanceof ApiError && err.code === "network") {
          setStatus("signedOut"); // keep the token: it may still be valid once the API is back
          return;
        }
        storeToken(null);
        setToken(null);
        setStatus("signedOut");
      });
    return () => controller.abort();
  }, [token, user]);

  const login = useCallback(async (email: string, password: string) => {
    const issued = await authApi.login({ email: email.trim(), password });
    const me = await authApi.me(issued.access_token);
    storeToken(issued.access_token);
    setToken(issued.access_token);
    setUser(me);
    setStatus("signedIn");
    return me;
  }, []);

  const register = useCallback(
    (displayName: string, email: string, password: string) =>
      authApi.register({ display_name: displayName.trim(), email: email.trim(), password }),
    [],
  );

  const logout = useCallback(() => {
    storeToken(null);
    setToken(null);
    setUser(null);
    setStatus("signedOut");
  }, []);

  const value = useMemo<Session>(
    () => ({ status, user, token, login, register, logout }),
    [status, user, token, login, register, logout],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
