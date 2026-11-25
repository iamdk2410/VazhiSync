/** The animated street scene in the desktop auth hero: parallax skylines, the road, and the
 *  bus cruising with speed lines. */
import { BusArt } from "../brand/BusArt";
import { Road } from "./Scenery";
import { Skyline } from "./Skyline";

export function HeroScene() {
  return (
    <div aria-hidden="true" style={{ flex: "1 1 auto", position: "relative", minHeight: 380 }}>
      <Skyline preset="far" seconds={48} bottom={120} />
      <Skyline preset="near" seconds={22} bottom={120} />
      <Road />
      <div style={{ position: "absolute", left: "50%", bottom: 70, width: 520, marginLeft: -260 }}>
        <svg viewBox="-40 -6 260 100" width={520} height={200} style={{ display: "block", overflow: "visible" }}>
          <ellipse cx={110} cy={88} rx={104} ry={4} fill="#000000" opacity={0.35} />
          <g
            className="vs-anim"
            style={{ transformBox: "fill-box", transformOrigin: "right center", animation: "vs-lines 0.9s ease-in-out infinite" }}
          >
            <path d="M-30 30 H-6 M-38 58 H-6" stroke="var(--vs-coral)" strokeWidth={4} strokeLinecap="round" />
            <path d="M-22 44 H-6" stroke="var(--vs-sand)" strokeWidth={4} strokeLinecap="round" />
          </g>
          <BusArt tone="dark" motion="cruise" archFill="var(--vs-navy-deep)" />
        </svg>
      </div>
    </div>
  );
}
