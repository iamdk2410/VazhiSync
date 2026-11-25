/** Form building blocks shared by every form in the app (the design's field style). */
import { useState, type ChangeEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";

import styles from "./Form.module.css";
import { AlertIcon, CheckIcon, EyeIcon, EyeOffIcon } from "./icons";

const cx = (...names: (string | false | undefined)[]) => names.filter(Boolean).join(" ");

export type FieldState = "neutral" | "error" | "ok";

interface TextFieldProps {
  id: string;
  label: string;
  icon: ReactNode;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  autoComplete?: string;
  placeholder?: string;
  state?: FieldState;
  /** shown under the field as an alert while `state` is "error" */
  error?: string;
  describedBy?: string;
  trailing?: ReactNode;
  /** the mobile sign-up's tighter spacing */
  compact?: boolean;
  /** anything under the field (hints, live status) */
  children?: ReactNode;
  maxLength?: number;
}

export function TextField({
  id, label, icon, value, onChange, type = "text", autoComplete, placeholder,
  state = "neutral", error, describedBy, trailing, compact, children, maxLength,
}: TextFieldProps) {
  const errorId = `${id}-error`;
  const showError = state === "error" && !!error;
  const described = [describedBy, showError ? errorId : undefined].filter(Boolean).join(" ");
  return (
    <div className={cx(styles.field, compact && styles.compact, state === "error" && styles.error, state === "ok" && styles.ok)}>
      <label htmlFor={id} className={styles.label}>{label}</label>
      <div className={styles.control}>
        <span className={styles.leadingIcon}>{icon}</span>
        <input
          id={id}
          className={cx(styles.input, !!trailing && styles.withTrailing)}
          type={type}
          autoComplete={autoComplete}
          placeholder={placeholder}
          value={value}
          maxLength={maxLength}
          onChange={(e: ChangeEvent<HTMLInputElement>) => onChange(e.target.value)}
          aria-invalid={state === "error"}
          aria-describedby={described || undefined}
        />
        {trailing}
      </div>
      {showError && (
        <span id={errorId} role="alert" className={cx(styles.message, styles.messageError)}>{error}</span>
      )}
      {children}
    </div>
  );
}

interface PasswordToggleProps {
  shown: boolean;
  onToggle: () => void;
  /** "password" or "passwords" (sign-up toggles both fields) */
  noun?: string;
}

export function PasswordToggle({ shown, onToggle, noun = "password" }: PasswordToggleProps) {
  return (
    <button
      type="button"
      className={styles.toggle}
      onClick={onToggle}
      aria-label={`${shown ? "Hide" : "Show"} ${noun}`}
      aria-pressed={shown}
    >
      {shown ? <EyeOffIcon /> : <EyeIcon />}
    </button>
  );
}

/** A password field with its own show/hide state (or a shared one via `shown`/`onToggle`). */
export function PasswordField(
  props: Omit<TextFieldProps, "type" | "trailing"> & {
    shown?: boolean;
    onToggle?: () => void;
    withToggle?: boolean;
    toggleNoun?: string;
  },
) {
  const { shown: shownProp, onToggle, withToggle = true, toggleNoun, ...rest } = props;
  const [own, setOwn] = useState(false);
  const shown = shownProp ?? own;
  const toggle = onToggle ?? (() => setOwn((s) => !s));
  return (
    <TextField
      {...rest}
      type={shown ? "text" : "password"}
      trailing={withToggle ? <PasswordToggle shown={shown} onToggle={toggle} noun={toggleNoun} /> : undefined}
    />
  );
}

export function FieldMessage({
  id, tone, icon, children, live,
}: {
  id?: string;
  tone: "error" | "ok" | "muted";
  icon?: "check" | "alert";
  children: ReactNode;
  live?: boolean;
}) {
  const toneClass = tone === "error" ? styles.messageError : tone === "ok" ? styles.messageOk : styles.messageMuted;
  return (
    <span id={id} className={cx(styles.message, styles.messageRow, toneClass)} aria-live={live ? "polite" : undefined}>
      {icon === "check" && <CheckIcon />}
      {icon === "alert" && <AlertIcon />}
      {children}
    </span>
  );
}

/** A fixed-height polite live region for status that comes and goes (e.g. "Passwords match"). */
export function LiveSlot({ id, children }: { id: string; children?: ReactNode }) {
  return <div id={id} aria-live="polite" className={styles.liveSlot}>{children}</div>;
}

export function PrimaryButton({
  children, busy, busyLabel, type = "submit",
}: { children: ReactNode; busy?: boolean; busyLabel?: string; type?: "submit" | "button" }) {
  return (
    <button type={type} className={styles.button} disabled={busy} aria-busy={busy || undefined}>
      {busy && busyLabel ? busyLabel : children}
    </button>
  );
}

/** A primary-looking link (e.g. "Go to sign in"). */
export function PrimaryLink({
  to, children, className, state,
}: { to: string; children: ReactNode; className?: string; state?: unknown }) {
  return <Link to={to} state={state} className={cx(styles.button, className)}>{children}</Link>;
}

export function FormFooter({
  text, linkText, to, tall, className,
}: { text: string; linkText: string; to: string; tall?: boolean; className?: string }) {
  return (
    <p className={cx(styles.footer, className)}>
      {text}{" "}
      <Link to={to} className={cx(styles.footerLink, tall && styles.footerLinkTall)}>{linkText}</Link>
    </p>
  );
}

/** A server-side problem (wrong password, email taken, network down...). */
export function FormAlert({ children }: { children: ReactNode }) {
  return (
    <div role="alert" className={styles.alert}>
      <AlertIcon />
      <span>{children}</span>
    </div>
  );
}
