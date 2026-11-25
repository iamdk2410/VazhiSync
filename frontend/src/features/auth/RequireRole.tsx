import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import type { Role } from "../../api/client";
import { ROLE_HOME, useSession } from "./session";

/** Lets only the given role in. Others go to their own home; signed-out visitors to sign in.
 *  (The server enforces roles too - this only keeps people on the right screens.) */
export function RequireRole({ role, children }: { role: Role; children: ReactNode }) {
  const { status, user } = useSession();
  const location = useLocation();
  if (status === "loading") return null;
  if (status !== "signedIn" || !user) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }
  if (user.role !== role) return <Navigate to={ROLE_HOME[user.role]} replace />;
  return <>{children}</>;
}
