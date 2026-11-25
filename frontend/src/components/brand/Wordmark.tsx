import type { CSSProperties } from "react";

import styles from "./Wordmark.module.css";

interface WordmarkProps {
  /** font size in px */
  size: number;
  /** "dark" = sand text on navy, "light" = navy text on sand */
  tone?: "dark" | "light";
  className?: string;
  style?: CSSProperties;
}

/** "Vazhi" + coral "Sync", Poppins 800 italic. */
export function Wordmark({ size, tone = "light", className, style }: WordmarkProps) {
  return (
    <div
      className={[styles.wordmark, styles[tone], className].filter(Boolean).join(" ")}
      style={{ fontSize: size, ...style }}
    >
      Vazhi<span className={styles.sync}>Sync</span>
    </div>
  );
}

interface TaglineProps {
  size: number;
  tone?: "dark" | "light";
  spacing?: string;
  className?: string;
  style?: CSSProperties;
}

export function Tagline({ size, tone = "light", spacing = "0.22em", className, style }: TaglineProps) {
  return (
    <div
      className={[styles.tagline, styles[tone], className].filter(Boolean).join(" ")}
      style={{ fontSize: size, letterSpacing: spacing, ...style }}
    >
      Bus bunching control advisor
    </div>
  );
}
