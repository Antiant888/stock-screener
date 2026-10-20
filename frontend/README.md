# Stock Scanner Frontend

React 18/Vite SPA for the live stock screener, static demo shell, market dashboards, scans, stock detail, watchlists, operations, validation, optional themes, and optional assistant workflows.

References:

- [Project overview](../README.md)
- [Live app guide](../docs/LIVE_APP_GUIDE.md)
- [Static site guide](../docs/STATIC_SITE.md)
- [Architecture](../docs/ARCHITECTURE.md)
- [Backend README](../backend/README.md)

## Setup

```bash
npm install
npm run dev       # Vite dev server on :5173
npm run build     # production build
npm run lint      # ESLint
npm run test:run  # Vitest once
```

The Vite dev server proxies `/api` to `http://127.0.0.1:8000`, so the backend should be running for live-app work.

## Stack

| Library | Purpose |
|---|---|
| React 18 + React Router 6 | SPA routing and UI composition |
| Vite | Dev server and production build |
| MUI | Component system and icons |
| TanStack Query | Server state, caching, polling, invalidation |
| TanStack Virtual | Row virtualization for scan tables and large result lists |
| Recharts | Sparklines, breadth, exposure, and dashboard charts |
| lightweight-charts | Candlestick and chart-modal views |
| axios | HTTP client |
| date-fns | Date formatting |
| @hello-pangea/dnd | Watchlist drag/drop |
| react-markdown | Assistant message rendering |

## App Shape

`src/App.jsx` is the route source of truth. User-facing route documentation belongs in the [Live App Guide](../docs/LIVE_APP_GUIDE.md).

Current live routes:

- `/`
- `/scan`
- `/breadth`
- `/groups`
- `/validation`
- `/themes` when `features.themes` is enabled
- `/chatbot` when `features.chatbot` is enabled
- `/stocks/:ticker`
- `/operations`

The app also lazy-loads login, first-run bootstrap, and static-mode shells.

## Project Structure

```text
src/
  App.jsx                    Router and provider stack
  main.jsx                   React entry point
  api/                       Axios client plus backend route modules
  components/                Shared UI and domain components
  components/Layout/         App shell, nav, runtime controls
  contexts/                  Runtime, strategy profile, pipeline, assistant, color mode
  features/scan/             Scan page containers, filter state, presets
  features/themes/           Theme page containers and theme-specific UI
  pages/                     Top-level route components
  static/                    Static-site shell and read-only pages
  hooks/                     Reusable UI/data hooks
  utils/                     Pure formatting/filter/color helpers
  test/                      Test setup and fixtures
```

Keep new feature-heavy code under `features/<area>/` when it has page state, containers, and local helpers. Use `components/` for reusable UI shared across pages.

## Provider And State Rules

Provider stack lives in `App.jsx`:

```text
QueryClient -> ColorMode -> Runtime -> StrategyProfile -> Router
```

`PipelineProvider` wraps the routed app only when theme features are enabled.

Guidelines:

- Server data belongs in TanStack Query.
- Context is only for global UI/runtime state.
- Local component state is preferred for local UI controls.
- Avoid copying query data into context or module globals.
- Feature availability comes from runtime capabilities, not hard-coded frontend assumptions.

## API Client Convention

`src/api/client.js` sets the axios `baseURL`.

| Mode | Base URL |
|---|---|
| Vite dev | `/api`, proxied to `127.0.0.1:8000` |
| Docker/nginx | `/api`, proxied by nginx to backend |
| Custom build | `VITE_API_URL` override |

API modules must use `/v1/...` paths because the base URL already includes `/api`.

```javascript
// Correct
apiClient.get('/v1/themes/rankings');

// Wrong in Docker: becomes /api/api/v1/...
apiClient.get('/api/v1/themes/rankings');
```

Themes requests receive a longer timeout in the shared client. Normal API requests use the default timeout.

## Routing And Feature Gates

Routes are lazy-loaded with `React.lazy()` and `<Suspense>`.

Feature-gated UI should check runtime capabilities before rendering nav items, controls, or routes. Current gated surfaces include:

- Themes
- Assistant/chatbot
- Task controls
- API docs links

Server login and bootstrap flows are handled by `RuntimeContext` and the app-level guards in `App.jsx`.

## Static Mode

The static shell under `src/static/` reads JSON bundles from `/static-data/*` and must not call live `/api` endpoints. See [Static Site Guide](../docs/STATIC_SITE.md) for the read-only behavior contract.

## Styling

- MUI theme tokens are defined in `App.jsx`.
- Dark mode is the default; light mode is supported.
- Keep operational screens dense and scannable.
- Prefer MUI icon buttons and compact controls over explanatory text.
- Reuse existing table, chart, modal, and layout patterns before adding new primitives.

## Testing

```bash
npm run test:run
npm run lint
npm run build
npm run test:smoke
```

Use focused Vitest files while iterating:

```bash
npm run test:run -- src/pages/OperationsPage.test.jsx
npm run test:run -- src/features/scan/filterOptions.test.js
```

Smoke tests use Playwright and expect a reachable app/backend according to the test target.
