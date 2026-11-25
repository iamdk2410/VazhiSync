import { LogoMark } from "../components/brand/LogoMark";
import { PrimaryLink } from "../components/form/fields";

export function NotFound() {
  return (
    <main
      style={{
        minHeight: "100vh", display: "flex", flexDirection: "column", alignItems: "center",
        justifyContent: "center", gap: 18, padding: 24, textAlign: "center",
      }}
    >
      <LogoMark variant="panel" width={168} height={84} />
      <h1 style={{ margin: 0, fontFamily: "var(--vs-font-display)", fontSize: 30 }}>This stop isn’t on the route</h1>
      <p style={{ margin: 0, color: "var(--vs-muted)", fontSize: 16 }}>The page you asked for doesn’t exist.</p>
      <div style={{ width: 240 }}>
        <PrimaryLink to="/">Back to VazhiSync</PrimaryLink>
      </div>
    </main>
  );
}
