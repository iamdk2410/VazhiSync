/**
 * The rest of the scenery from the designs: the road, the splash waves, the mobile wave edges
 * and the decorative corner shapes. All decorative (aria-hidden).
 */
import { useId } from "react";

/** The road under the auth hero: deep navy with a blue kerb and moving lane dashes. */
export function Road() {
  const id = `dash-${useId().replace(/:/g, "")}`;
  return (
    <div
      aria-hidden="true"
      style={{
        position: "absolute", left: 0, right: 0, bottom: 0, height: 120,
        background: "var(--vs-navy-deep)", borderTop: "4px solid var(--vs-blue)", overflow: "hidden",
      }}
    >
      <div style={{ position: "absolute", left: 0, top: 54, width: "calc(100% + 80px)", height: 6 }}>
        <svg className="vs-anim" width="100%" height={6} style={{ display: "block", animation: "vs-pan80 0.45s linear infinite" }}>
          <defs>
            <pattern id={id} width={80} height={6} patternUnits="userSpaceOnUse">
              <rect x={0} y={0} width={40} height={6} rx={3} fill="var(--vs-sand)" opacity={0.5} />
            </pattern>
          </defs>
          <rect width="100%" height={6} fill={`url(#${id})`} />
        </svg>
      </div>
    </div>
  );
}

/** The splash screen's two rolling waves (blue behind, coral in front). */
export function SplashWaves({ mobile = false }: { mobile?: boolean }) {
  const back = mobile
    ? { h: 150, vb: "0 0 1440 150", s: 14, d: "M0 70 C120 30 240 30 360 70 C480 110 600 110 720 70 C840 30 960 30 1080 70 C1200 110 1320 110 1440 70 V150 H0 Z" }
    : { h: 180, vb: "0 0 2880 180", s: 22, d: "M0 80 C240 30 480 30 720 80 C960 130 1200 130 1440 80 C1680 30 1920 30 2160 80 C2400 130 2640 130 2880 80 V180 H0 Z" };
  const front = mobile
    ? { h: 90, vb: "0 0 1440 90", s: 9, d: "M0 55 C100 85 260 85 360 45 C460 5 620 5 720 55 C820 85 980 85 1080 45 C1180 5 1340 5 1440 55 V90 H0 Z" }
    : { h: 110, vb: "0 0 2880 110", s: 13, d: "M0 70 C200 110 520 110 720 60 C920 10 1240 10 1440 70 C1640 110 1960 110 2160 60 C2360 10 2680 10 2880 70 V110 H0 Z" };
  return (
    <div aria-hidden="true" style={{ position: "absolute", left: 0, right: 0, bottom: 0, height: back.h, overflow: "hidden", pointerEvents: "none" }}>
      {[{ ...back, fill: "var(--vs-blue)" }, { ...front, fill: "var(--vs-coral)" }].map((w) => (
        <svg
          key={w.fill}
          className="vs-anim"
          viewBox={w.vb}
          preserveAspectRatio="none"
          style={{ position: "absolute", left: 0, bottom: 0, width: "200%", height: w.h, display: "block", animation: `vs-wave ${w.s}s linear infinite` }}
        >
          <path d={w.d} fill={w.fill} />
        </svg>
      ))}
    </div>
  );
}

/** The coral wave line that closes the mobile headers, blending into the sand page. */
export function HeaderWaveEdge() {
  return (
    <svg
      className="vs-anim"
      aria-hidden="true"
      viewBox="0 0 1440 48"
      preserveAspectRatio="none"
      style={{ position: "absolute", left: 0, bottom: -1, width: "200%", height: 48, display: "block", animation: "vs-wave 10s linear infinite" }}
    >
      <path
        d="M0 18 C120 -2 240 -2 360 18 C480 38 600 38 720 18 C840 -2 960 -2 1080 18 C1200 38 1320 38 1440 18"
        fill="none"
        stroke="var(--vs-coral)"
        strokeWidth={4}
      />
      <path
        d="M0 28 C120 8 240 8 360 28 C480 48 600 48 720 28 C840 8 960 8 1080 28 C1200 48 1320 48 1440 28 V48 H0 Z"
        fill="var(--vs-page)"
      />
    </svg>
  );
}

/** The blue and coral waves along the bottom of the mobile pages. */
export function FooterWaves() {
  return (
    <svg aria-hidden="true" viewBox="0 0 390 56" preserveAspectRatio="none" style={{ display: "block", width: "100%", height: 56, marginTop: -56, pointerEvents: "none" }}>
      <path d="M0 56 V36 C80 20 150 44 230 30 C300 18 350 10 390 20 V56 Z" fill="var(--vs-blue)" opacity={0.2} />
      <path d="M180 56 C240 50 300 34 340 34 C364 34 380 30 390 26 V56 Z" fill="var(--vs-coral)" />
    </svg>
  );
}

/** The coral stripe (and blue sliver) in the hero's top-right corner. */
export function HeroCornerStripe({ size = "large" }: { size?: "large" | "small" }) {
  if (size === "small") {
    return (
      <svg aria-hidden="true" viewBox="0 0 120 150" width={120} height={150} style={{ position: "absolute", top: 0, right: 0, display: "block" }}>
        <path d="M84 0 H118 L30 150 H-4 Z" fill="var(--vs-coral)" />
      </svg>
    );
  }
  return (
    <svg aria-hidden="true" viewBox="0 0 240 300" width={240} height={300} style={{ position: "absolute", top: 0, right: 0, display: "block" }}>
      <path d="M168 0 H232 L56 300 H-8 Z" fill="var(--vs-coral)" />
      <path d="M240 0 V40 L214 84 Z" fill="var(--vs-blue)" />
    </svg>
  );
}

/** The soft shapes in the form panel's corners (top-left blue, bottom-right blue + coral). */
export function PanelCorners() {
  return (
    <>
      <svg aria-hidden="true" viewBox="0 0 360 140" width={360} height={140} style={{ position: "absolute", right: 0, bottom: 0, display: "block" }}>
        <path d="M0 140 C90 124 150 62 240 58 C300 56 340 32 360 0 V140 Z" fill="var(--vs-blue)" opacity={0.22} />
        <path d="M110 140 C190 128 250 92 300 88 C330 86 350 72 360 58 V140 Z" fill="var(--vs-coral)" />
      </svg>
      <svg aria-hidden="true" viewBox="0 0 200 90" width={200} height={90} style={{ position: "absolute", left: 0, top: 0, display: "block" }}>
        <path d="M0 0 H200 C150 8 110 40 60 56 C30 66 10 80 0 90 Z" fill="var(--vs-blue)" opacity={0.12} />
      </svg>
    </>
  );
}
