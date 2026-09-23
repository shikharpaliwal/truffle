# Truffle — frontend

React + Vite client for the Truffle scanner. Three views: the ranked table of the
universe, a coin detail page with metric history, and the "Today's truffles" panel
of sharpest score risers.

## Develop

```
npm install
npm run dev          # http://localhost:5173
```

From the repo root, `make web` runs the dashboard alone and `make dev` runs it
alongside the API.

`/api` is proxied to the FastAPI backend at `http://127.0.0.1:8000` (see
`vite.config.js`). Keep the literal IPv4 address: Node 22 resolves `localhost` to
`::1`, which uvicorn does not bind. Nothing else needs configuring when the
backend is up.

## Data and failure states

The app only ever renders what the API returns; there is no fixture or offline mode.

- Backend unreachable (transport error, or the dev proxy's 502) or any 5xx: the view
  is replaced by an explicit "couldn't reach the API" message.
- Backend reachable but never scanned: `200` with `run_date: null`, shown as the
  "No runs yet" empty state.
- 4xx (e.g. an unknown coin): the backend's own `detail` is surfaced.

## Build & check

```
npm run build
npm run lint
npm run preview
```

## Notes

- Routing: React Router. Table state (`date`, `sort`, `order`, `q`, `min_volume`)
  lives in the URL query string and maps 1:1 onto the API's own params, so sorting
  and filtering are server-side and every view is linkable.
- Data fetching: TanStack Query.
- Styling: plain CSS in `src/styles.css`, driven by custom properties on `:root`.
  Dark mode follows `prefers-color-scheme` and can be overridden by a toggle that
  persists to `localStorage`.
- Charts are hand-rolled SVG (`src/components/MetricChart.jsx`) — no chart library.

- Deployment: routes are client-side, so a static host needs an SPA rewrite
  (all paths → `index.html`).
