/**
 * The VazhiSync mark: a route arc ending in a stop, and the bus arriving under it.
 *
 * variant:
 * - "splash": the loading-screen choreography (arc draws, stop pops with a ring, bus drives
 *   in and brakes); `iteration` loops it or plays it once.
 * - "header": the mobile sign-in header (bus cruising, speed lines pulsing).
 * - "compact": the mobile sign-up header (smaller, heavier strokes, no shadow).
 * - "panel": the desktop form panel on sand (static, light bus).
 */
import type { CSSProperties } from "react";

import { BusArt } from "./BusArt";

type Variant = "splash" | "header" | "compact" | "panel";

interface Props {
  variant: Variant;
  width: number | string;
  height?: number | string;
  iteration?: string;
  className?: string;
  style?: CSSProperties;
}

const ARC = "M58 64 C92 -6 236 -10 276 66";
const fillBox: CSSProperties = { transformBox: "fill-box", transformOrigin: "center" };

function SpeedLines({ middle, width, motion }: { middle: string; width: number; motion?: string }) {
  return (
    <g
      className={motion ? "vs-anim" : undefined}
      style={motion ? { transformBox: "fill-box", transformOrigin: "right center", animation: motion } : undefined}
    >
      <path d="M8 80 H32 M0 108 H32" stroke="var(--vs-coral)" strokeWidth={width} strokeLinecap="round" />
      <path d="M14 94 H32" stroke={middle} strokeWidth={width} strokeLinecap="round" />
    </g>
  );
}

export function LogoMark({ variant, width, height, iteration = "infinite", className, style }: Props) {
  const svgProps = {
    viewBox: "0 0 300 150",
    width,
    height,
    "aria-hidden": true as const,
    className,
    style: { display: "block", overflow: "visible", ...style } as CSSProperties,
  };

  if (variant === "splash") {
    return (
      <svg {...svgProps}>
        <path
          className="vs-anim"
          d={ARC}
          pathLength={1}
          strokeDasharray={1}
          fill="none"
          stroke="var(--vs-coral)"
          strokeWidth={9}
          strokeLinecap="round"
          style={{ strokeDashoffset: 0, animation: `vs-arc 6s ${iteration} both` }}
        />
        <circle
          className="vs-anim"
          cx={276}
          cy={66}
          r={8}
          fill="none"
          stroke="var(--vs-coral)"
          strokeWidth={3}
          style={{ ...fillBox, opacity: 0, animation: `vs-ring 6s ease-out ${iteration} both` }}
        />
        <circle
          className="vs-anim"
          cx={276}
          cy={66}
          r={8}
          fill="var(--vs-sand)"
          stroke="var(--vs-coral)"
          strokeWidth={4}
          style={{ ...fillBox, animation: `vs-dot 6s ${iteration} both` }}
        />
        <g className="vs-anim" style={{ animation: `vs-drive 6s ${iteration} both` }}>
          <SpeedLines middle="var(--vs-sand)" width={4.5} motion={`vs-speed 6s ${iteration} both`} />
          <ellipse cx={150} cy={134} rx={104} ry={5} fill="#000000" opacity={0.3} />
          <g transform="translate(40 46)">
            <BusArt tone="dark" motion="arrive" iteration={iteration} />
          </g>
        </g>
      </svg>
    );
  }

  if (variant === "panel") {
    return (
      <svg {...svgProps}>
        <path d={ARC} fill="none" stroke="var(--vs-coral)" strokeWidth={9} strokeLinecap="round" />
        <circle cx={276} cy={66} r={8} fill="var(--vs-page)" stroke="var(--vs-coral)" strokeWidth={4} />
        <SpeedLines middle="var(--vs-blue)" width={4.5} />
        <ellipse cx={150} cy={134} rx={104} ry={5} fill="var(--vs-navy)" opacity={0.12} />
        <g transform="translate(40 46)">
          <BusArt tone="light" archFill="var(--vs-navy)" />
        </g>
      </svg>
    );
  }

  const compact = variant === "compact";
  return (
    <svg {...svgProps}>
      <path d={ARC} fill="none" stroke="var(--vs-coral)" strokeWidth={compact ? 10 : 9} strokeLinecap="round" />
      <circle
        cx={276}
        cy={66}
        r={compact ? 9 : 8}
        fill="var(--vs-sand)"
        stroke="var(--vs-coral)"
        strokeWidth={4}
      />
      <SpeedLines
        middle="var(--vs-sand)"
        width={compact ? 5 : 4.5}
        motion="vs-lines 0.9s ease-in-out infinite"
      />
      {!compact && <ellipse cx={150} cy={134} rx={104} ry={5} fill="#000000" opacity={0.3} />}
      <g transform="translate(40 46)">
        <BusArt tone="dark" motion="cruise" glints={!compact} details={!compact} />
      </g>
    </svg>
  );
}
