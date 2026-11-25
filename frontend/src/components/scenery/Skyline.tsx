/**
 * A panning city skyline: a repeating SVG pattern scrolled sideways forever.
 * The presets are the designs' own building outlines.
 */
import { useId, type CSSProperties } from "react";

interface Building { x: number; y: number; w: number; h: number }
interface Window { x: number; y: number; w: number; h: number; color: string; opacity: number }

interface Preset {
  width: number; // pattern width (also how far it pans per loop)
  height: number;
  color: string;
  buildings: Building[];
  windows?: Window[];
}

const coral = "var(--vs-coral)";
const sand = "var(--vs-sand)";

const SKYLINES = {
  splash: {
    width: 480, height: 200, color: "var(--vs-skyline-1)",
    buildings: [
      { x: 0, y: 90, w: 56, h: 110 }, { x: 60, y: 40, w: 44, h: 160 }, { x: 108, y: 110, w: 72, h: 90 },
      { x: 184, y: 60, w: 38, h: 140 }, { x: 226, y: 20, w: 52, h: 180 }, { x: 282, y: 100, w: 60, h: 100 },
      { x: 346, y: 56, w: 42, h: 144 }, { x: 392, y: 120, w: 84, h: 80 },
    ],
  },
  splashMobile: {
    width: 480, height: 170, color: "var(--vs-skyline-1)",
    buildings: [
      { x: 0, y: 70, w: 56, h: 100 }, { x: 60, y: 30, w: 44, h: 140 }, { x: 108, y: 90, w: 72, h: 80 },
      { x: 184, y: 50, w: 38, h: 120 }, { x: 226, y: 14, w: 52, h: 156 }, { x: 282, y: 84, w: 60, h: 86 },
      { x: 346, y: 46, w: 42, h: 124 }, { x: 392, y: 100, w: 84, h: 70 },
    ],
  },
  far: {
    width: 480, height: 220, color: "var(--vs-skyline-far)",
    buildings: [
      { x: 0, y: 100, w: 56, h: 120 }, { x: 60, y: 40, w: 44, h: 180 }, { x: 108, y: 120, w: 72, h: 100 },
      { x: 184, y: 70, w: 38, h: 150 }, { x: 226, y: 20, w: 52, h: 200 }, { x: 282, y: 110, w: 60, h: 110 },
      { x: 346, y: 60, w: 42, h: 160 }, { x: 392, y: 130, w: 84, h: 90 },
    ],
  },
  near: {
    width: 360, height: 140, color: "var(--vs-skyline-near)",
    buildings: [
      { x: 0, y: 60, w: 70, h: 80 }, { x: 74, y: 20, w: 40, h: 120 }, { x: 118, y: 80, w: 90, h: 60 },
      { x: 212, y: 40, w: 50, h: 100 }, { x: 266, y: 90, w: 40, h: 50 }, { x: 310, y: 30, w: 46, h: 110 },
    ],
    windows: [
      { x: 84, y: 34, w: 6, h: 8, color: coral, opacity: 0.7 },
      { x: 98, y: 58, w: 6, h: 8, color: sand, opacity: 0.35 },
      { x: 226, y: 54, w: 6, h: 8, color: sand, opacity: 0.35 },
      { x: 320, y: 46, w: 6, h: 8, color: coral, opacity: 0.7 },
      { x: 134, y: 94, w: 6, h: 8, color: sand, opacity: 0.35 },
    ],
  },
  mobileHeader: {
    width: 360, height: 120, color: "var(--vs-skyline-far)",
    buildings: [
      { x: 0, y: 50, w: 70, h: 70 }, { x: 74, y: 14, w: 40, h: 106 }, { x: 118, y: 70, w: 90, h: 50 },
      { x: 212, y: 34, w: 50, h: 86 }, { x: 266, y: 78, w: 40, h: 42 }, { x: 310, y: 24, w: 46, h: 96 },
    ],
    windows: [
      { x: 84, y: 30, w: 5, h: 7, color: coral, opacity: 0.6 },
      { x: 322, y: 40, w: 5, h: 7, color: sand, opacity: 0.3 },
    ],
  },
} satisfies Record<string, Preset>;

interface Props {
  preset: keyof typeof SKYLINES;
  /** seconds per pattern width */
  seconds: number;
  /** distance from the bottom of the positioned parent */
  bottom: number;
  className?: string;
  style?: CSSProperties;
}

export function Skyline({ preset, seconds, bottom, className, style }: Props) {
  const p: Preset = SKYLINES[preset];
  const id = `sky-${useId().replace(/:/g, "")}`;
  return (
    <div
      aria-hidden="true"
      className={className}
      style={{
        position: "absolute",
        left: 0,
        bottom,
        width: `calc(100% + ${p.width}px)`,
        height: p.height,
        pointerEvents: "none",
        ...style,
      }}
    >
      <svg
        className="vs-anim"
        width="100%"
        height={p.height}
        style={{ display: "block", animation: `vs-pan${p.width} ${seconds}s linear infinite` }}
      >
        <defs>
          <pattern id={id} width={p.width} height={p.height} patternUnits="userSpaceOnUse">
            {p.buildings.map((b) => (
              <rect key={`${b.x}-${b.y}`} x={b.x} y={b.y} width={b.w} height={b.h} fill={p.color} />
            ))}
            {p.windows?.map((w) => (
              <rect
                key={`w${w.x}-${w.y}`}
                x={w.x}
                y={w.y}
                width={w.w}
                height={w.h}
                fill={w.color}
                opacity={w.opacity}
              />
            ))}
          </pattern>
        </defs>
        <rect width="100%" height={p.height} fill={`url(#${id})`} />
      </svg>
    </div>
  );
}
