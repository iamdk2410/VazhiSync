import { useState, type FormEvent } from "react";

import { ApiError } from "../../api/client";
import {
  FieldMessage,
  FormAlert,
  FormFooter,
  LiveSlot,
  PasswordField,
  PrimaryButton,
  PrimaryLink,
  TextField,
} from "../../components/form/fields";
import { CheckIcon, LockIcon, MailIcon, PersonIcon } from "../../components/form/icons";
import styles from "./AuthForms.module.css";
import { useSession } from "./session";
import { NAME_MAX, PASSWORD_MAX, PASSWORD_MIN, checkSignup } from "./validation";

function signupProblem(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === "conflict") return "An account with this email already exists. Sign in instead.";
    if (err.code === "rate_limited") return "Too many attempts. Wait a minute and try again.";
    if (err.code === "network") return "Can't reach VazhiSync right now. Check your connection and try again.";
    if (err.code === "invalid") return err.message;
  }
  return "Something went wrong creating your account. Please try again.";
}

type Variant = "desktop" | "mobile";

export function SignupForm({ variant, onCreated }: { variant: Variant; onCreated: (email: string) => void }) {
  const { register } = useSession();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [again, setAgain] = useState("");
  const [shown, setShown] = useState(false);
  const [tried, setTried] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const mobile = variant === "mobile";
  const id = (base: string) => (mobile ? `m-su-${base}` : `su-${base}`);
  const c = checkSignup(name, email, password, again);
  const nameErr = tried && !c.nameOk;
  const emailErr = tried && !c.emailOk;
  const passwordErr = tried && !c.lengthOk;
  const againEmpty = tried && again.length === 0;
  const againErr = c.mismatch || againEmpty;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (!c.valid) {
      setTried(true);
      return;
    }
    setProblem(null);
    setBusy(true);
    try {
      const user = await register(name, email, password);
      onCreated(user.email);
    } catch (err) {
      setProblem(signupProblem(err));
      setBusy(false);
    }
  }

  const hintTone = passwordErr ? "error" : c.lengthOk ? "ok" : "muted";

  return (
    <form
      noValidate
      onSubmit={submit}
      className={[styles.form, styles.signup, mobile && styles.mobile].filter(Boolean).join(" ")}
    >
      <div className={styles.intro}>
        <h2 className={styles.title}>Create your account</h2>
        <p className={styles.lead}>
          Sign-up is for passengers. Drivers and control officers get their accounts from the
          transport office.
        </p>
      </div>

      {problem && <FormAlert>{problem}</FormAlert>}

      <div className={styles.fields}>
        <TextField
          id={id("name")}
          label="Your name"
          icon={<PersonIcon />}
          autoComplete="name"
          placeholder="How should we greet you?"
          value={name}
          onChange={setName}
          maxLength={NAME_MAX}
          state={nameErr ? "error" : "neutral"}
          error="Enter your name."
          compact={mobile}
        />
        <TextField
          id={id("email")}
          label="Email address"
          icon={<MailIcon />}
          type="email"
          autoComplete="email"
          placeholder="you@example.com"
          value={email}
          onChange={setEmail}
          state={emailErr ? "error" : "neutral"}
          error="Enter a valid email address."
          compact={mobile}
        />
        <PasswordField
          id={id("password")}
          label="Create password"
          icon={<LockIcon />}
          autoComplete="new-password"
          placeholder={`At least ${PASSWORD_MIN} characters`}
          value={password}
          onChange={setPassword}
          maxLength={PASSWORD_MAX}
          state={passwordErr ? "error" : c.lengthOk ? "ok" : "neutral"}
          describedBy={id("pw-hint")}
          shown={shown}
          onToggle={() => setShown((s) => !s)}
          toggleNoun="passwords"
          compact={mobile}
        >
          <FieldMessage id={id("pw-hint")} tone={hintTone} icon={c.lengthOk ? "check" : undefined}>
            Use at least {PASSWORD_MIN} characters.
          </FieldMessage>
        </PasswordField>
        <PasswordField
          id={id("password2")}
          label="Re-enter password"
          icon={<LockIcon />}
          autoComplete="new-password"
          placeholder="Type it again"
          value={again}
          onChange={setAgain}
          maxLength={PASSWORD_MAX}
          state={againErr ? "error" : c.matchOk ? "ok" : "neutral"}
          describedBy={id("match")}
          shown={shown}
          withToggle={false}
          compact={mobile}
        >
          <LiveSlot id={id("match")}>
            {c.matchOk && (
              <FieldMessage tone="ok" icon="check">Passwords match</FieldMessage>
            )}
            {c.mismatch && (
              <FieldMessage tone="error" icon="alert">Passwords don’t match</FieldMessage>
            )}
            {againEmpty && <FieldMessage tone="error">Re-enter your password.</FieldMessage>}
          </LiveSlot>
        </PasswordField>
      </div>

      <PrimaryButton busy={busy} busyLabel="Creating account…">Create account</PrimaryButton>

      <FormFooter
        text="Already have an account?"
        linkText="Sign in"
        to="/login"
        tall={mobile}
        className={mobile ? styles.pushDown : undefined}
      />
    </form>
  );
}

export function SignupSuccess({ variant, email }: { variant: Variant; email: string }) {
  const mobile = variant === "mobile";
  return (
    <div role="status" className={[styles.success, mobile && styles.mobile].filter(Boolean).join(" ")}>
      <div className={styles.badge}>
        <CheckIcon size={30} />
      </div>
      <h2 className={styles.title}>Account created</h2>
      <p className={styles.successText}>You can now sign in with {email}.</p>
      <PrimaryLink to="/login" state={{ email }} className={styles.stretch}>
        Go to sign in
      </PrimaryLink>
    </div>
  );
}
