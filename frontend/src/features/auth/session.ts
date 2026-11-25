/** Auth state shared through React context (the provider lives in AuthProvider.tsx). */
import { createContext, useContext } from "react";

import type { User } from "../../api/auth";
import type { Role } from "../../api/client";

export type AuthStatus = "loading" | "signedOut" | "signedIn";

export interface Session {
  status: AuthStatus;
  user: User | null;
  token: string | null;
  /** signs in and returns the user (whose role decides where to go) */
  login: (email: string, password: string) => Promise<User>;
  register: (displayName: string, email: string, password: string) => Promise<User>;
  logout: () => void;
}

export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession must be used inside <AuthProvider>");
  return session;
}

/** Where each role lands after signing in. */
export const ROLE_HOME: Record<Role, string> = {
  officer: "/officer",
  driver: "/driver",
  passenger: "/passenger",
};

export const ROLE_LABEL: Record<Role, string> = {
  officer: "Control officer",
  driver: "Driver",
  passenger: "Passenger",
};

export const TOKEN_KEY = "vazhisync.token";
