/**
 * The VazhiSync bus, drawn once and reused everywhere (an SVG <g>, placed inside a parent
 * <svg>). Coordinates are the design's own (the bus spans about x 6..216, y 2..86).
 *
 * - tone "dark": on navy backgrounds (splash, auth hero, mobile headers) - cream body,
 *   window glints, headlight, tail light and mirror.
 * - tone "light": on the sand form panel - white body with a hairline outline, no details.
 * - motion "cruise": the body bobs and the wheels roll (sign-in / sign-up scenes).
 * - motion "arrive": the splash choreography - the body brakes and the wheels spin down.
 */
import type { CSSProperties } from "react";

export type BusTone = "dark" | "light";
export type BusMotion = "none" | "cruise" | "arrive";

interface Props {
  tone: BusTone;
  motion?: BusMotion;
  /** colour behind the wheel arches (the surface the bus is on) */
  archFill?: string;
  /** splash only: "infinite" loops the 6 s choreography, "1" plays it once */
  iteration?: string;
  /** window glints (the diagonal highlights); dark tone only */
  glints?: boolean;
  /** headlight, tail light and mirror; dark tone only */
  details?: boolean;
  /** draw the wheels inside the moving body (splash) or outside it (cruise) */
  className?: string;
}

const anim = (value: string): CSSProperties => ({ animation: value });
const fillBox: CSSProperties = { transformBox: "fill-box", transformOrigin: "center" };

function Wheel({ x, motion, iteration }: { x: number; motion: BusMotion; iteration: string }) {
  if (motion === "none") {
    return (
      <>
        <circle cx={x} cy={74} r={12} fill="var(--vs-tyre)" />
        <circle cx={x} cy={74} r={6.5} fill="var(--vs-bus-roof)" />
      </>
    );
  }
  const spin =
    motion === "arrive" ? `vs-spin 6s ${iteration} both` : "vs-roll 0.5s linear infinite";
  return (
    <g transform={`translate(${x} 74)`}>
      <g className="vs-anim" style={{ ...fillBox, ...anim(spin) }}>
        <circle r={12} fill="var(--vs-tyre)" />
        <circle r={6.5} fill="var(--vs-bus-roof)" />
        <path d="M0 -6.5 V6.5 M-6.5 0 H6.5" stroke="var(--vs-tyre)" strokeWidth={2} />
      </g>
    </g>
  );
}

export function BusArt({
  tone,
  motion = "none",
  archFill = "var(--vs-navy)",
  iteration = "infinite",
  glints = tone === "dark",
  details = tone === "dark",
}: Props) {
  const body = tone === "dark" ? "var(--vs-bus-white)" : "var(--vs-white)";
  const bar = body;
  const bodyMotion =
    motion === "cruise"
      ? anim("vs-bob 0.8s ease-in-out infinite")
      : motion === "arrive"
        ? { transformBox: "fill-box" as const, transformOrigin: "50% 100%", ...anim(`vs-brake 6s ${iteration} both`) }
        : undefined;

  const shell = (
    <>
      <rect x={34} y={2} width={76} height={8} rx={3} fill="var(--vs-bus-roof)" />
      <path
        d="M18 8 H182 Q199 8 205 22 L211 48 Q213 54 213 60 V66 Q213 74 205 74 H14 Q6 74 6 66 V20 Q6 8 18 8 Z"
        fill={body}
        {...(tone === "light"
          ? { stroke: "var(--vs-navy)", strokeOpacity: 0.18, strokeWidth: 1.5 }
          : {})}
      />
      <rect x={14} y={16} width={134} height={26} rx={4} fill="var(--vs-blue)" />
      <rect x={46} y={16} width={3} height={26} fill={bar} />
      <rect x={80} y={16} width={3} height={26} fill={bar} />
      <rect x={114} y={16} width={3} height={26} fill={bar} />
      {glints && (
        <>
          <path d="M20 40 L34 18 H42 L28 40 Z" fill="#FFFFFF" opacity={0.16} />
          <path d="M88 40 L102 18 H107 L93 40 Z" fill="#FFFFFF" opacity={0.16} />
        </>
      )}
      <rect x={154} y={16} width={22} height={50} rx={3} fill="var(--vs-blue)" />
      <rect x={164.25} y={16} width={1.5} height={50} fill={bar} />
      <path d="M182 16 H197 Q203 16 206 26 L210 46 H182 Z" fill="var(--vs-blue)" />
      <rect x={184} y={19} width={16} height={5} rx={1.5} fill="var(--vs-coral)" />
      {glints && <path d="M186 44 L194 20 H198 L190 44 Z" fill="#FFFFFF" opacity={0.16} />}
      <rect x={6} y={48} width={148} height={8} fill="var(--vs-coral)" />
      <rect x={176} y={48} width={35} height={8} fill="var(--vs-coral)" />
      {details && (
        <>
          <rect x={203} y={60} width={9} height={5} rx={2} fill="#FFFFFF" />
          <rect x={6} y={58} width={4} height={9} rx={1} fill="var(--vs-coral)" />
          <path
            d="M209 24 L216 20 V30"
            fill="none"
            stroke="var(--vs-navy)"
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </>
      )}
      <path d="M33 74 A15 15 0 0 1 63 74 Z" fill={archFill} />
      <path d="M161 74 A15 15 0 0 1 191 74 Z" fill={archFill} />
    </>
  );

  const wheels = (
    <>
      <Wheel x={48} motion={motion} iteration={iteration} />
      <Wheel x={176} motion={motion} iteration={iteration} />
    </>
  );

  if (motion === "arrive") {
    // the splash: wheels brake together with the body
    return (
      <g className="vs-anim" style={bodyMotion}>
        {shell}
        {wheels}
      </g>
    );
  }
  return (
    <>
      <g className={motion === "cruise" ? "vs-anim" : undefined} style={bodyMotion}>
        {shell}
      </g>
      {wheels}
    </>
  );
}
