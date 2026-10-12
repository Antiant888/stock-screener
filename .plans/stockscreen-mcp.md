# stockscreen-mcp: no-install MCP server over the published static data

## Goal

`uvx stockscreen-mcp` gives any MCP client (Claude Desktop/Code, Cursor, VS Code, …) the project's daily screens, RS ratings, market health, breadth and group rankings for 12 markets. It needs no API keys, Docker or server: it reads the JSON that the static site already publishes to GitHub Pages.

## Decisions (agreed 2026-10-12)

- **Location:** `stockscreen-mcp/` in this repo, with its own `pyproject.toml`, so listings and links point at this repo.
- **Distribution:** PyPI from the start (`uvx stockscreen-mcp`), released by a tag workflow with PyPI trusted publishing.
- **Name:** `stockscreen-mcp`.
- **v1 tools:** screens and stock lookup, market health and breadth, group rankings. RRG is deferred.

## Data access (`data.py`)

- Source: `https://xang1234.github.io/stock-screener/static-data/` (override with `STOCKSCREEN_DATA_URL`).
- The root `manifest.json` names the current `data_root` (`g/<generation>/`). Only the current generation is deployed (#504).
- **Manifest:** re-read after 600s, matching the Pages `Cache-Control: max-age=600`.
- **Data files:** cached in memory per `data_root`, and dropped when the generation changes. Heavy, unused fields (`*_sparkline_data`, `field_availability`) are pruned on load. A full US scan is about 7 MB gzipped on the wire and 74 MB as JSON.
- **Expired generation:** a 404 under the current root refreshes the manifest. If the root moved, the whole load is retried once.
- **Locking:** one lock serialises loads. Tools are plain `def`, which MCPServer runs in a worker thread.
- **Errors:** network failures and unknown or unavailable markets become `ToolError` (visible to the model).
- No disk cache in v1. Add one if repeated cold-start downloads become a complaint.

## Screen semantics (`screens.py`)

- **Evaluator:** a port of `frontend/src/features/scan/filterExpressionEvaluator.js`, including its field aliases: `price`→`current_price`, `price_change_1d`→`pct_day`, `perf_week`→`pct_week`, `perf_month`→`pct_month`, `market_cap_usd`, `listing_aware_volume`.
- **A preset replaces the default filter**, as on the site. This was verified against the live publish of 2026-10-09: the preset alone gives Minervini 37, CANSLIM 72 and VCP 10, the site's chip counts; preset plus the default filter would give 7, 17 and 3.
- **Hidden presets:** as on the site, presets using opportunity-state fields (`correction_survivor`, `resilience_score`, `action_state`) are hidden for markets without `features.opportunity_state`.
- **Custom filters** are `{field, min, max}` or `{field, equals}`. They AND with the chosen preset. Unknown fields are rejected with suggestions.
- **Sorting:** the preset's `sort_by`/`sort_order`, otherwise the manifest default (`composite_score` desc). Nulls sort last.

## Tools (`server.py`, `mcp>=2.3,<3`, `MCPServer`, all read-only)

| Tool | Returns |
|---|---|
| `list_markets()` | markets, as-of dates, publication status, available features |
| `list_screens(market)` | preset id, name, description, conditions, sort |
| `run_screen(market, screen?, filters?, sort_by?, descending?, limit)` | match count, plus trimmed rows (about 15 key fields, plus the fields used to filter and sort) |
| `get_stock(symbol, market)` | the full row (minus heavy fields), plus the preset screens it currently passes |
| `market_overview(market)` | exposure score/stance/distribution days/FTD/VIX, StockBee breadth (latest + 5 days), key markets, top groups, scan counts |
| `group_rankings(market, group?, limit)` | ranked groups with 1W/1M/3M/6M rank changes and weekly movers; with `group`, its history and constituents by RS |

Every result carries `market` and `as_of_date`. Server instructions explain that RS ratings are 1–99 percentiles within each market, that the data is an end-of-day snapshot, and that it is not investment advice.

**Mid-session behaviour:** the server only reads the published end-of-day snapshot. It never calls price providers, never sees a partial bar, and reports `as_of_date` (the last completed session the publish covers).

## Packaging and release

- **Layout:** `stockscreen-mcp/pyproject.toml` (hatchling, `[project.scripts] stockscreen-mcp`), `src/stockscreen_mcp/{__init__,data,screens,server}.py`, `tests/`.
- **CI:** a job runs the package tests with `uv`.
- **Release:** `.github/workflows/stockscreen-mcp-release.yml` on tags `stockscreen-mcp-v*`. It checks that the tag matches the `pyproject` version, then tests, `uv build`, and publishes through PyPI trusted publishing (`id-token: write`).
- **One-time PyPI setup (owner):** add a pending trusted publisher for project `stockscreen-mcp`, repository `xang1234/stock-screener`, workflow `stockscreen-mcp-release.yml`, environment `pypi`.
- **Docs:** the package README (client config snippets and the `mcp-name` line for the official MCP registry), a README section, and a pointer in `docs/MCP_INTEGRATION.md`.

## Out of scope for v1

- RRG.
- Disk cache.
- `server.json` and MCP registry / Smithery / Glama submissions, which come after the first PyPI release.
- Options data.
