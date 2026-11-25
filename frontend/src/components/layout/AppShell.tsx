/** The signed-in frame: navy bar with the brand, who is signed in, and sign out. */
import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { LogoMark } from "../brand/LogoMark";
import { Wordmark } from "../brand/Wordmark";
import { SignOutIcon } from "../form/icons";
import { ROLE_HOME, ROLE_LABEL, useSession } from "../../features/auth/session";
import styles from "./AppShell.module.css";

export function AppShell({ children }: { children: ReactNode }) {
  const { user, logout } = useSession();
  const navigate = useNavigate();
  return (
    <div className={styles.shell}>
      <header className={styles.bar}>
        <Link to={user ? ROLE_HOME[user.role] : "/"} className={styles.brand} aria-label="VazhiSync home">
          <LogoMark variant="compact" width={64} height={32} />
          <Wordmark size={24} tone="dark" />
        </Link>
        <span className={styles.spacer} />
        {user && (
          <div className={styles.who}>
            <span className={styles.name}>{user.display_name}</span>
            <span className={styles.role}>{ROLE_LABEL[user.role]}</span>
          </div>
        )}
        <button
          type="button"
          className={styles.signOut}
          onClick={() => {
            logout();
            navigate("/login", { replace: true });
          }}
          aria-label="Sign out"
        >
          <SignOutIcon />
          <span className={styles.signOutText}>Sign out</span>
        </button>
      </header>
      <div className={styles.stripe} aria-hidden="true" />
      <main className={styles.main}>{children}</main>
    </div>
  );
}

export interface HomeCard {
  title: string;
  text: string;
}

/** The temporary role home: a greeting and what this role will get (built in B14-B16). */
export function RoleHome({ greeting, lead, cards }: { greeting: string; lead: string; cards: HomeCard[] }) {
  const { user } = useSession();
  return (
    <AppShell>
      <h1 className={styles.title}>
        {greeting}
        {user ? `, ${user.display_name}` : ""}
      </h1>
      <p className={styles.lead}>{lead}</p>
      <div className={styles.cards}>
        {cards.map((c) => (
          <section key={c.title} className={styles.card}>
            <span className={styles.soon}>Coming soon</span>
            <h2 className={styles.cardTitle}>{c.title}</h2>
            <p className={styles.cardText}>{c.text}</p>
          </section>
        ))}
      </div>
    </AppShell>
  );
}
