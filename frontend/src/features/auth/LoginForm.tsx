import { useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { ApiError } from "../../api/client";
import { FormAlert, FormFooter, PasswordField, PrimaryButton, TextField } from "../../components/form/fields";
import { LockIcon, MailIcon } from "../../components/form/icons";
import styles from "./AuthForms.module.css";
import { ROLE_HOME, useSession } from "./session";
import { checkLogin } from "./validation";

function loginProblem(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "unauthorized") return "That email and password don't match an account.";
    if (err.code === "rate_limited") return "Too many sign-in attempts. Wait a minute and try again.";
    if (err.code === "network") return "Can't reach VazhiSync right now. Check your connection and try again.";
    if (err.code === "invalid") return err.message;
  }
  return "Something went wrong signing in. Please try again.";
}

export function LoginForm({ variant }: { variant: "desktop" | "mobile" }) {
  const { login } = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const prefill = (location.state as { email?: string } | null)?.email ?? "";
  const [email, setEmail] = useState(prefill);
  const [password, setPassword] = useState("");
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const check = checkLogin(email, password);
  const emailErr = tried && !check.emailOk;
  const passwordErr = tried && !check.passwordOk;
  const mobile = variant === "mobile";

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (!check.valid) {
      setTried(true);
      return;
    }
    setTried(false);
    setProblem(null);
    setBusy(true);
    try {
      const user = await login(email, password);
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from.startsWith(ROLE_HOME[user.role]) ? from : ROLE_HOME[user.role], { replace: true });
    } catch (err) {
      setProblem(loginProblem(err));
      setBusy(false);
    }
  }

  return (
    <form
      noValidate
      onSubmit={submit}
      className={[styles.form, styles.login, mobile && styles.mobile].filter(Boolean).join(" ")}
    >
      <div className={styles.intro}>
        <h2 className={styles.title}>Welcome back</h2>
        <p className={styles.lead}>Sign in to continue to your dashboard.</p>
      </div>

      {problem && <FormAlert>{problem}</FormAlert>}

      <div className={styles.fields}>
        <TextField
          id={mobile ? "m-login-email" : "login-email"}
          label="Email address"
          icon={<MailIcon />}
          type="email"
          autoComplete="email"
          placeholder="you@example.com"
          value={email}
          onChange={setEmail}
          state={emailErr ? "error" : "neutral"}
          error="Enter a valid email address."
        />
        <PasswordField
          id={mobile ? "m-login-password" : "login-password"}
          label="Password"
          icon={<LockIcon />}
          autoComplete="current-password"
          placeholder="Your password"
          value={password}
          onChange={setPassword}
          state={passwordErr ? "error" : "neutral"}
          error="Enter your password."
        />
      </div>

      <PrimaryButton busy={busy} busyLabel="Signing in…">Sign in</PrimaryButton>

      <FormFooter
        text="New to VazhiSync?"
        linkText="Create an account"
        to="/signup"
        tall={mobile}
        className={mobile ? styles.pushDown : undefined}
      />
    </form>
  );
}
