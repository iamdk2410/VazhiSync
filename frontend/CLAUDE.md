# VazhiSync frontend

React 19 + Vite + TypeScript (strict). Talks to the backend only through the HTTP API (and,
from B13 on, the WebSocket), never the database or MCP servers. Root rules: `../CLAUDE.md`.

## Commands (run from `frontend/`)
- `npm run dev`: dev server on http://localhost:5173 (the backend's `CORS_ORIGINS` allows it)
- `npm run build`: `tsc -b` plus the Vite production build
- `npm run lint` / `npm run typecheck` / `npm test`: ESLint, types, Vitest (jsdom)
- `npm run gen:api`: regenerate `src/api/schema.d.ts` from the running backend's `/openapi.json`.
  Run it after any backend API change. Never edit the generated file by hand.

Configuration: `VITE_API_URL` (see `.env.example`, default `http://localhost:8000`).

## Layout (`src/`)
```
api/          client.ts (fetch wrapper → typed ApiError codes), auth.ts, schema.d.ts (generated)
styles/       tokens.css (every colour/font/radius), animations.css (the designs' vs-* keyframes), global.css
components/
  brand/      BusArt (the one bus drawing), LogoMark, Wordmark/Tagline
  scenery/    Skyline, HeroScene, Scenery (road, waves, corner shapes)
  form/       fields.tsx (TextField, PasswordField, PrimaryButton, FormAlert…), icons.tsx
  layout/     AuthLayout (desktop/mobile auth frames), AppShell (signed-in frame)
features/auth/ session.ts (context, ROLE_HOME), AuthProvider, RequireRole, LoginForm, SignupForm, validation.ts
pages/        SplashPage, AuthPages, RoleHomes (placeholders until B14–B16), NotFound
hooks/        useMediaQuery (useIsMobile at 720 px, usePrefersReducedMotion)
test/         setup + render helpers
```

## Rules
- The designs live in `../VazhiSync-project/*.dc.html`. Use the colours and fonts from
  `styles/tokens.css`, never a raw hex in a component (SVG art included: use `var(--vs-…)`; plain white/black overlays with opacity are the only exception).
- Anything animated gets the `vs-anim` class so `prefers-reduced-motion` stops it.
- Desktop and mobile are separate layouts picked by `useIsMobile()`. Forms are shared and take
  a `variant`.
- Every request goes through `api/client.ts` with types from `schema.d.ts`.
- Roles are only for routing here. The server enforces them (`require_role`).
- Tests sit next to the code (`*.test.ts(x)`), using Testing Library queries by role and label.
