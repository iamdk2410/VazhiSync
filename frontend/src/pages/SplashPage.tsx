/**
 * The loading screen (Main.dc.html / MobileSplash.dc.html): the bus drives in under the route
 * arc, the wordmark wipes in, the progress bar fills. It plays once, then continues to the
 * signed-in user's home or to sign in - on its own when the animation ends, or straight away
 * on click / tap / Enter.
 */
import { useEffect, useState, type CSSProperties } from "react";
import { Link, useNavigate } from "react-router-dom";

import { LogoMark } from "../components/brand/LogoMark";
import { Wordmark } from "../components/brand/Wordmark";
import { SplashWaves } from "../components/scenery/Scenery";
import { Skyline } from "../components/scenery/Skyline";
import { ROLE_HOME, useSession } from "../features/auth/session";
import { useIsMobile, usePrefersReducedMotion } from "../hooks/useMediaQuery";
import styles from "./SplashPage.module.css";

export const SPLASH_MS = 6000;
const ONCE = "1";

const play = (name: string, easing = ""): CSSProperties => ({
  animation: `${name} 6s ${easing} ${ONCE} both`.replace(/\s+/g, " "),
});

export function SplashPage({ durationMs = SPLASH_MS }: { durationMs?: number }) {
  const mobile = useIsMobile();
  const reducedMotion = usePrefersReducedMotion();
  const { status, user } = useSession();
  const navigate = useNavigate();
  const [done, setDone] = useState(false);

  const destination = status === "signedIn" && user ? ROLE_HOME[user.role] : "/login";

  useEffect(() => {
    const timer = window.setTimeout(() => setDone(true), reducedMotion ? 1200 : durationMs);
    return () => window.clearTimeout(timer);
  }, [durationMs, reducedMotion]);

  useEffect(() => {
    if (done && status !== "loading") navigate(destination, { replace: true });
  }, [done, status, destination, navigate]);

  return (
    <div className={styles.page} style={{ ["--vs-drive-from" as string]: mobile ? "-700px" : "-1100px" }}>
      <Link
        to={destination}
        replace
        aria-label={status === "signedIn" ? "VazhiSync. Continue to your dashboard" : "VazhiSync. Continue to sign in"}
        className={[styles.stage, mobile && styles.mobile].filter(Boolean).join(" ")}
      >
        <Skyline
          preset={mobile ? "splashMobile" : "splash"}
          seconds={mobile ? 60 : 70}
          bottom={mobile ? 90 : 110}
        />
        <SplashWaves mobile={mobile} />

        <div className={styles.logo}>
          <LogoMark variant="splash" width="100%" iteration={ONCE} />
        </div>

        <Wordmark
          size={mobile ? 52 : 80}
          tone="dark"
          className={`vs-anim ${styles.word}`}
          style={play("vs-word", "ease-out")}
        />
        <p className={`vs-anim ${styles.tag}`} style={play("vs-tag", "ease-out")}>
          Bus bunching control advisor
        </p>

        <div className={`vs-anim ${styles.progress}`} style={play("vs-fade", "ease-out")}>
          <div aria-hidden="true" className={styles.track}>
            <div className={`vs-anim ${styles.bar}`} style={play("vs-bar", "ease-in-out")} />
          </div>
          <span className={styles.loading} role="status">
            {status === "loading" ? "Checking your session…" : "Loading routes…"}
          </span>
        </div>
        <span className={`vs-anim ${styles.hint}`} style={play("vs-hint", "ease-out")}>
          {mobile ? "Tap to continue" : "Click anywhere to continue"}
        </span>
      </Link>
    </div>
  );
}
