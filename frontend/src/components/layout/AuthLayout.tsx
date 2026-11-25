/**
 * The frame around the auth forms, from the designs:
 * - desktop: a navy hero (eyebrow, headline, copy, animated street) beside a sand form panel
 *   with the logo block;
 * - mobile: a navy header (centred logo for sign-in, compact with a back arrow for sign-up)
 *   above the form, with waves along the bottom.
 * `AuthLayout` picks the right one for the screen.
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { LogoMark } from "../brand/LogoMark";
import { Tagline, Wordmark } from "../brand/Wordmark";
import { BackIcon } from "../form/icons";
import { HeroScene } from "../scenery/HeroScene";
import { FooterWaves, HeaderWaveEdge, HeroCornerStripe, PanelCorners } from "../scenery/Scenery";
import { Skyline } from "../scenery/Skyline";
import { useIsMobile } from "../../hooks/useMediaQuery";
import styles from "./AuthLayout.module.css";

export interface HeroCopy {
  eyebrow: string;
  title: string;
  lead: string;
}

interface Props {
  hero: HeroCopy;
  /** "login": centred mobile header; "signup": compact header with a back link */
  kind: "login" | "signup";
  children: (variant: "desktop" | "mobile") => ReactNode;
}

export function AuthLayout(props: Props) {
  const mobile = useIsMobile();
  return mobile ? <MobileAuthLayout {...props} /> : <DesktopAuthLayout {...props} />;
}

export function DesktopAuthLayout({ hero, kind, children }: Props) {
  const signup = kind === "signup";
  return (
    <div className={styles.desktop}>
      <section className={styles.hero} aria-labelledby="auth-hero-title">
        <HeroCornerStripe />
        <div className={styles.heroCopy}>
          <span className={styles.eyebrow}>{hero.eyebrow}</span>
          <h1 id="auth-hero-title" className={styles.heroTitle}>{hero.title}</h1>
          <p className={styles.heroLead}>{hero.lead}</p>
        </div>
        <HeroScene />
      </section>

      <main className={[styles.panel, signup && styles.panelTight].filter(Boolean).join(" ")}>
        <PanelCorners />
        <div className={styles.panelInner}>
          <div className={styles.brandBlock}>
            <LogoMark variant="panel" width={signup ? 140 : 168} height={signup ? 70 : 84} />
            <Wordmark size={signup ? 30 : 36} />
            <Tagline size={11} />
          </div>
          {children("desktop")}
        </div>
      </main>
    </div>
  );
}

export function MobileAuthLayout({ hero, kind, children }: Props) {
  const signup = kind === "signup";
  return (
    <div className={styles.mobile}>
      <header
        className={[styles.mHeader, signup ? styles.mHeaderCompact : styles.mHeaderCentered].join(" ")}
      >
        <h1 className="vs-visually-hidden">{hero.title}</h1>
        {signup ? (
          <>
            <svg aria-hidden="true" viewBox="0 0 120 150" width={96} height={120} style={{ position: "absolute", top: 0, right: 0, display: "block" }}>
              <path d="M84 0 H118 L30 150 H-4 Z" fill="var(--vs-coral)" />
            </svg>
            <Link to="/login" aria-label="Back to sign in" className={styles.back}>
              <BackIcon />
            </Link>
            <div className={styles.compactBrand}>
              <LogoMark variant="compact" width={124} height={62} style={{ flex: "0 0 auto" }} />
              <div className={styles.compactWords}>
                <Wordmark size={28} tone="dark" />
                <Tagline size={9} tone="dark" spacing="0.18em" />
              </div>
            </div>
          </>
        ) : (
          <>
            <Skyline preset="mobileHeader" seconds={30} bottom={40} />
            <HeroCornerStripe size="small" />
            <LogoMark variant="header" width={210} height={105} className={styles.mRelative} />
            <Wordmark size={38} tone="dark" className={styles.mRelative} />
            <Tagline size={10} tone="dark" className={styles.mRelative} />
          </>
        )}
        <HeaderWaveEdge />
      </header>

      <main className={[styles.mMain, signup && styles.mMainTight].filter(Boolean).join(" ")}>
        {children("mobile")}
      </main>
      <FooterWaves />
    </div>
  );
}
