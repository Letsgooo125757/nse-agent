# NSE Investment Copilot: Phase 1 (Data Pipeline)

This is the data foundation for a personal AI research assistant covering the
Nairobi Securities Exchange. Phase 1 collects and cleans the data and stores it
in PostgreSQL. Later phases add the agent that reasons over it.

```
            ┌───────────────┐   ┌──────────────┐   ┌────────────────┐
 sources →  │ afx (prices)  │   │ RSS feeds    │   │ CSV imports    │
            │ market + per- │   │ local/global │   │ history, FY    │
            │ stock pages   │   │ news         │   │ results, divs, │
            └──────┬────────┘   └──────┬───────┘   │ macro          │
                   │ PriceBatch        │ NewsItem  └──────┬─────────┘
                   ▼                   ▼                  ▼
            ┌──────────────────────────────────────────────────────┐
 loaders →  │ idempotent upserts · ticker & topic tagging · run log│
            └──────────────────────────┬───────────────────────────┘
                                       ▼
            ┌──────────────────────────────────────────────────────┐
 Postgres → │ companies · daily_prices · index_values ·            │
            │ financial_statements · corporate_actions ·           │
            │ news_articles · news_ticker_links · macro_* ·        │
            │ ingestion_runs · investor_profiles · transactions    │
            │ views: v_latest_prices, v_valuation,                 │
            │        v_upcoming_dividends, v_ticker_news,          │
            │        v_pipeline_health                             │
            └──────────────────────────────────────────────────────┘
                                       ▲
                     Phase 2+: agent tools query these views
```

## Quick start

Requirements: Python 3.10+, and Docker (or any PostgreSQL 15+).

```bash
docker compose up -d                 # Postgres on localhost:5432 (user/pass/db: nse)
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                 # then put your email in HTTP_USER_AGENT

nse-agent init-db                    # tables, views, 65 securities, sectors, macro series
nse-agent check-feeds                # confirm each news feed works from your network
nse-agent ingest-prices --history    # today's snapshot + ~10 days' history per traded stock
nse-agent ingest-news
nse-agent status                     # row counts + last run of every job
```

## Commands

| Command | What it does |
|---|---|
| `init-db` | Creates or updates the schema and loads reference data. Safe to re-run. |
| `seed` | Reloads `nse_agent/data/*.csv` after you edit companies or aliases. |
| `ingest-prices [--history] [--tickers ..]` | Fetches the price snapshot for all listings plus NASI. `--history` also backfills recent days per stock. |
| `ingest-news [--source ..]` | Pulls the RSS feeds, dedupes by URL, and tags each article with tickers and topics. |
| `check-feeds` | Tests every feed without saving anything. |
| `import-prices FILE [--ticker X]` | Loads historical prices from CSV. Flexible headers (`Date`/`trade_date`, `Close`/`Price`, ...). |
| `import-financials FILE` | Loads annual and interim results. Feeds `v_valuation` (P/E, yield, ROE, payout). |
| `import-dividends FILE` | Loads dividend history. Feeds `v_upcoming_dividends`. |
| `import-macro FILE` | Loads CBK rate, T-bill yields, USD/KES, inflation, Brent and the Fed rate. |
| `retag-news` | Re-runs tagging over all stored news after you change aliases. |
| `daily` | Runs `ingest-prices` then `ingest-news`. This is the command to schedule. |
| `status` | Shows data counts and pipeline health. |

Header-only CSV templates are in `templates/`. Enter amounts in full KES, not
KES '000 as many annual reports show them.

## Scheduling

NSE equities trade 09:30–15:00 EAT, Monday to Friday. Run `daily` after the close.

```cron
# mkdir -p logs; crontab -e   (machine clock set to Africa/Nairobi)
30 15 * * 1-5  cd /path/to/nse-agent && .venv/bin/nse-agent daily >> logs/daily.log 2>&1
0  8,12,20 * * *  cd /path/to/nse-agent && .venv/bin/nse-agent ingest-news >> logs/news.log 2>&1
```

On Windows, use Task Scheduler with the same commands.

## Data sources, and the caveats that matter

**Prices (afx.kwayisi.org).** This free public mirror of NSE prices is fine for
a personal project. Before relying on it:

- **Check freshness.** When I looked at the site during development, it was
  still showing a November 2024 session. The pipeline reads the session date
  off the page and flags the run as *stale* when it is more than
  `STALE_AFTER_DAYS` old, so `nse-agent status` will tell you. If it stays
  stale, switch sources: add an adapter in `nse_agent/sources/` (for example
  Mansa API, or a broker export) or use `import-prices`.
- **Intraday runs** are flagged, because the "price" before 15:00 is not the
  closing price. A later run the same day overwrites it.
- **Untraded securities** get a row with `volume = 0` and the previous price
  carried forward. This matches how the NSE publishes its daily price list.
- **Redistribution needs a licence.** The NSE requires a data distribution
  licence for anyone passing its data to third parties. Keep this personal, or
  buy a licensed feed before you share it.

**News.** Capital FM Business is a verified RSS feed with full article text.
The Google News search feeds give headlines and links only, and may be blocked
on some networks. Run `check-feeds`, and edit `nse_agent/data/feeds.json` to
add or remove feeds.

**Fundamentals, dividends and macro data** are imported from CSV in Phase 1.
Structured Kenyan fundamentals are the hard part of this project. Extracting
them from annual-report PDFs is planned for Phase 3.

## Design decisions

- **Sources don't touch the database, and loaders don't touch the network.**
  Each adapter turns a page into plain records (`models.py`), so it can be
  tested against a saved fixture. Adding a new price source is one file.
- **Tables are located by header text, not by position or CSS class.** If a
  site redesign removes the expected columns, the run fails loudly and is
  logged as `failed` instead of loading shifted numbers.
- **Every write is an upsert**, so any job can be re-run. Upserts never
  replace a known value with NULL, so a close-only snapshot won't erase
  OHLC data you imported.
- **Every run is logged** in `ingestion_runs`, with status
  `ok`/`partial`/`failed`, warnings and errors. The run log is committed
  separately, so failures are recorded even when the data rolls back.
- **Unknown tickers are skipped with a warning**, not auto-created. New
  listings (for example Quickmart, if it lists) are added to `companies.csv`
  and loaded with `seed`.
- **News tagging is explainable.** It uses phrase matching on company aliases.
  ALL-CAPS aliases such as `KCB` or `CBK` are case-sensitive, and matches
  respect word boundaries (`I&M` and `M-Pesa` work). Each link stores the alias
  that matched and a relevance score: 0.9+ when the company is in the headline,
  about 0.3 for a passing mention. Ten macro **topics** (`interest_rates`,
  `currency`, `oil_energy`, `tax_fiscal`, ...) tag market-wide drivers.
- **Money columns are `NUMERIC`, never float.**

## Before you rely on the reference data

`nse_agent/data/companies.csv` lists 65 securities, but some fields need checking:

- Sector assignments come from the NSE's sector classification as best I
  know it. Verify them against the current NSE listings page.
- `market_segment` is only filled where it is certain (REIT, ETF, preference
  shares). Fill in MIMS/AIMS/GEMS from the NSE site.
- `is_active = false` is set for ARM Cement and Mumias. Check for other
  suspensions.
- `shares_outstanding` is empty. Fill it from annual reports so that
  `market_cap` in `v_latest_prices` populates.

## Tests

```bash
pytest -q
```

There are 37 tests. The database tests use an embedded throwaway Postgres
(`pgserver`), so no setup is needed. To run them against another server, set
`TEST_DATABASE_URL`. The tests drop the `public` schema there, so never point
it at real data. Network calls are replaced by fixtures in `tests/fixtures/`.

## What's next

- **Phase 2:** investor profile questionnaire, allocation rules, a portfolio
  tracker on `transactions`, and alerts for price moves, book closures and
  concentration.
- **Phase 3:** an LLM agent with tools over the views, per-article impact
  summaries, `pgvector` search over news and annual reports (the
  docker-compose image already includes it), and PDF extraction of
  financials.
- **Phase 4:** a web or WhatsApp front end.

For personal and educational use. This is not investment advice.
