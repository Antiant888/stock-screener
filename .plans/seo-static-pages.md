# Pre-rendered pages for search engines

## Goal

The static demo is a hash-routed single-page app (SPA), so Google indexes one empty page. This work generates plain HTML pages at stable URLs, built from each publish's own static JSON, so that searches like "NVDA RS rating", "minervini trend template stocks today" or "IBD group rankings" can land on the project.

## Decisions (agreed 2026-10-12)

- **Stock pages:** all 12 markets. Every *scannable* scan row, meaning complete data with an RS rating (US 8,803).
  - The approval was framed as "charted symbols (~22.7k)". But charted symbols are a display set (top 200 per preset plus top groups, `STATIC_CHART_*` in `static_site_export_service.py`), so they don't match the intent.
- **Screen pages:** a curated set for every market:
  - presets `minervini`, `canslim`, `vcp`, `se_cup_handle`, `se_high_tight_flag`, `new_highs`;
  - plus group rankings and market breadth.
- **Search Console:** the owner verifies the URL-prefix property `https://xang1234.github.io/stock-screener/` and submits `sitemap.xml`. The verification file is added once its name is known.
  - A project site can't serve the domain-root `robots.txt`, so the sitemap is not discoverable automatically.

## Design

`scripts/render_seo_pages.py` is a stdlib-only generator, run in `static-site-publish.yml` after "Build static frontend". It reads `frontend/dist/static-data` (root manifest, then `data_root`, then per-market pages) and writes HTML into `frontend/dist`.

- **Evaluator and sort:** it reuses `stockscreen-mcp/src/stockscreen_mcp/screens.py`, imported by path. That module is pure stdlib. Its full preset order matches the site's JS on the live publish. Installing the package would pull pydantic 2.12+ into the backend environment, which pins 2.5.
- **URLs** (under the site base path):

  | Page | Path |
  |---|---|
  | Hub | `/explore/` |
  | Market hub | `/markets/<m>/` |
  | Stock directory | `/stocks/<m>/` |
  | Stock | `/stocks/<m>/<SYMBOL>/` |
  | Screen | `/screens/<m>/<preset>/` |
  | Groups | `/groups/<m>/` |
  | Breadth | `/breadth/<m>/` |

  Plus `/sitemap.xml` and `/seo.css`.
- **Stock page content:**
  - title and h1 with symbol, name, RS rating and stage;
  - facts table: price, 1-day change, RS overall/1M/3M/12M, stage, composite/Minervini/CANSLIM scores, group and group rank, EPS/sales growth, distance from 52-week high, market cap;
  - an inline SVG sparkline from `price_sparkline_data`;
  - the curated screens it passes today, linked;
  - "as of" date, canonical URL, meta description and OG tags, a link to the app (`#/scan?market=<M>`), and a disclaimer.
- **Screen page:** the preset's description and readable rules, the match count, and up to 100 matches in the site's order, each linked to its stock page.
- **Groups page:** the ranked table with 1W/1M/3M changes.
- **Breadth page:** the latest numbers plus the last 10 days.
- **Market hub:** exposure stance, with links to everything for that market.
- **Discovery:** `sitemap.xml` with `lastmod` set to each market's `as_of_date`, submitted in Search Console, plus a README link to `/explore/`. The app itself is unchanged.
- **Screen tables** show the preset's sort column, so the order can be explained.
- **Safety:** every value goes through `html.escape`, and unusual symbol characters become `_XX` hex in paths (`BRK/B` → `BRK_2FB`).
- **Mid-session behaviour:** it only reads the published end-of-day snapshot. There are no provider calls and no partial bars, and every page shows its `as_of_date`.

## Size and time

About 5 KB per stock page against the 1 GB Pages limit. The current artifact is 194 MB compressed. Generation is a single pass over JSON that is already local.

## Out of scope

- Per-group pages and per-preset pages beyond the curated six.
- JSON-LD.
- Pages for symbols that aren't scannable.
