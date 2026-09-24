"""Command-line entry point: `nse-agent <command>` (or `python -m nse_agent`)."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

import psycopg

from . import commands, loaders
from .config import NAIROBI_TZ, get_settings
from .db import apply_schema, connect
from .sources import afx, csv_files, rss
from .portfolio import PortfolioError
from .sources.base import SourceError

log = logging.getLogger("nse_agent")

# NSE equities trade 09:30–15:00 EAT, Monday–Friday.
MARKET_CLOSE = dtime(15, 0)


# ---------------------------------------------------------------- commands


def cmd_init_db(args) -> int:
    s = get_settings()
    with connect() as conn:
        apply_schema(conn)
        counts = loaders.seed_reference_data(conn)
        loaders.sync_news_sources(conn, rss.load_feeds(s.feeds_path))
    print("Schema applied. Seeded:", ", ".join(f"{v} {k}" for k, v in counts.items()))
    return 0


def cmd_seed(args) -> int:
    s = get_settings()
    with connect() as conn:
        counts = loaders.seed_reference_data(conn)
        loaders.sync_news_sources(conn, rss.load_feeds(s.feeds_path))
    print("Seeded:", ", ".join(f"{v} {k}" for k, v in counts.items()))
    return 0


def price_warnings(as_of, now: datetime, stale_after_days: int) -> list[str]:
    """Flag snapshots that are too old, or taken before today's close."""
    out = []
    today = now.date()
    if (today - as_of).days > stale_after_days:
        out.append(f"Stale data: source is showing {as_of}, {(today - as_of).days} days old")
    if as_of == today and now.time() < MARKET_CLOSE and now.weekday() < 5:
        out.append("Snapshot taken during trading hours; prices are not final closes — re-run after 15:00 EAT")
    return out


def cmd_ingest_prices(args) -> int:
    s = get_settings()
    now = datetime.now(NAIROBI_TZ)
    with connect() as conn, loaders.ingestion_run(conn, "prices", afx.SOURCE) as run:
        batch = afx.fetch_market(s.afx_base_url, fallback_date=now.date())
        run.warnings += batch.warnings + price_warnings(batch.as_of, now, s.stale_after_days)
        loaders.upsert_prices(conn, batch.bars, run)
        loaders.upsert_indices(conn, batch.indices, run)

        if args.history:
            tickers = args.tickers or sorted(b.ticker for b in batch.bars if b.volume)
            for t in tickers:
                try:
                    loaders.upsert_prices(conn, afx.fetch_stock_history(s.afx_base_url, t), run)
                except SourceError as exc:
                    run.warnings.append(f"{t}: {exc}")
                time.sleep(args.delay)  # be polite to a free site
    print(f"Prices as of {batch.as_of}: {run.rows_written} rows written")
    for w in run.warnings:
        print("  warning:", w)
    return 0


def cmd_ingest_news(args) -> int:
    s = get_settings()
    feeds = [f for f in rss.load_feeds(s.feeds_path) if f.enabled]
    if args.source:
        feeds = [f for f in feeds if f.source_code in args.source]
    failures = 0
    with connect() as conn:
        loaders.sync_news_sources(conn, feeds)
        conn.commit()
        tagger = loaders.build_tagger(conn)
        for feed in feeds:
            try:
                with loaders.ingestion_run(conn, "news", feed.source_code) as run:
                    items = rss.fetch_feed(feed)
                    links = loaders.upsert_news(conn, items, tagger, run)
                    conn.execute("UPDATE news_sources SET last_ok_at = now(), last_error = NULL "
                                 "WHERE source_code = %s", (feed.source_code,))
                print(f"{feed.source_code}: {len(items)} items, {run.rows_written} new, {links} ticker links")
            except SourceError as exc:
                failures += 1
                conn.execute("UPDATE news_sources SET last_error = %s WHERE source_code = %s",
                             (str(exc)[:500], feed.source_code))
                conn.commit()
                print(f"{feed.source_code}: FAILED — {exc}", file=sys.stderr)
    return 1 if failures == len(feeds) and feeds else 0


def cmd_check_feeds(args) -> int:
    s = get_settings()
    for feed in rss.load_feeds(s.feeds_path):
        try:
            items = rss.fetch_feed(feed)
            newest = max((i.published_at for i in items if i.published_at), default=None)
            print(f"OK    {feed.source_code:24} {len(items):3} items, newest {newest}")
        except SourceError as exc:
            print(f"FAIL  {feed.source_code:24} {exc}")
    return 0


def _run_import(job: str, path: Path, reader, writer) -> int:
    result = reader(path)
    with connect() as conn, loaders.ingestion_run(conn, job, path.name) as run:
        run.warnings += result.skipped[:20]
        if len(result.skipped) > 20:
            run.warnings.append(f"... and {len(result.skipped) - 20} more skipped lines")
        writer(conn, result.rows, run)
    print(f"{path.name}: {run.rows_written} rows written, {len(result.skipped)} lines skipped")
    for w in run.warnings[:10]:
        print("  warning:", w)
    return 0


def cmd_import_prices(args) -> int:
    return _run_import("import-prices", args.file,
                       lambda p: csv_files.read_prices_csv(p, ticker=args.ticker, source=f"csv:{p.name}"),
                       loaders.upsert_prices)


def cmd_import_financials(args) -> int:
    return _run_import("import-financials", args.file, csv_files.read_financials_csv, loaders.upsert_financials)


def cmd_import_dividends(args) -> int:
    return _run_import("import-dividends", args.file, csv_files.read_dividends_csv, loaders.upsert_dividends)


def cmd_import_macro(args) -> int:
    return _run_import("import-macro", args.file, csv_files.read_macro_csv, loaders.upsert_macro)


def cmd_retag_news(args) -> int:
    with connect() as conn:
        n = loaders.retag_all_news(conn, loaders.build_tagger(conn))
    print(f"Re-tagged all articles: {n} ticker links")
    return 0


def cmd_daily(args) -> int:
    """What cron runs after market close: prices, then news."""
    rc = 0
    try:
        cmd_ingest_prices(argparse.Namespace(history=False, tickers=None, delay=0))
    except Exception as exc:  # keep going to news even if prices fail
        print(f"prices FAILED — {exc}", file=sys.stderr)
        rc = 1
    rc |= cmd_ingest_news(argparse.Namespace(source=None))
    # then alerts for every profile
    with connect() as conn:
        pids = [r["id"] for r in conn.execute("SELECT id FROM investor_profiles ORDER BY id")]
    for pid in pids:
        commands.cmd_alerts_run(argparse.Namespace(profile=pid, date=None))
    return rc


def cmd_status(args) -> int:
    url = get_settings().database_url
    print(f"database             {'embedded (' + str(get_settings().pgdata_dir) + ')' if url == 'embedded' else url.split('@')[-1]}")
    with connect() as conn:
        counts = conn.execute("""
            SELECT (SELECT count(*) FROM companies)            AS companies,
                   (SELECT count(*) FROM daily_prices)         AS price_rows,
                   (SELECT max(trade_date) FROM daily_prices)  AS latest_price_date,
                   (SELECT count(*) FROM news_articles)        AS articles,
                   (SELECT count(*) FROM news_ticker_links)    AS ticker_links,
                   (SELECT count(*) FROM financial_statements) AS financial_rows,
                   (SELECT count(*) FROM macro_observations)   AS macro_rows
        """).fetchone()
        for k, v in counts.items():
            print(f"{k:20} {v}")
        print("\nLast run per job/source:")
        for r in conn.execute("SELECT * FROM v_pipeline_health ORDER BY job, source"):
            print(f"  {r['job']:18} {r['source']:24} {r['status']:8} "
                  f"{r['started_at']:%Y-%m-%d %H:%M}  wrote {r['rows_written']}"
                  + (f"  ERROR {r['error']}" if r["error"] else "")
                  + (f"  ({len(r['warnings'])} warnings)" if r["warnings"] else ""))
    return 0


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nse-agent", description="NSE investment research copilot")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create tables/views and load reference data").set_defaults(func=cmd_init_db)
    sub.add_parser("seed", help="reload companies, sectors, macro series, feeds").set_defaults(func=cmd_seed)

    sp = sub.add_parser("ingest-prices", help="fetch the latest NSE price snapshot")
    sp.add_argument("--history", action="store_true", help="also backfill recent days per stock")
    sp.add_argument("--tickers", nargs="*", help="limit --history to these tickers")
    sp.add_argument("--delay", type=float, default=1.0, help="seconds between per-stock requests")
    sp.set_defaults(func=cmd_ingest_prices)

    sp = sub.add_parser("ingest-news", help="fetch RSS feeds and tag articles")
    sp.add_argument("--source", nargs="*", help="only these source_codes")
    sp.set_defaults(func=cmd_ingest_news)

    sub.add_parser("check-feeds", help="test every configured feed without saving").set_defaults(func=cmd_check_feeds)

    for name, func, helptext in [
        ("import-prices", cmd_import_prices, "load historical prices from CSV"),
        ("import-financials", cmd_import_financials, "load annual/interim results from CSV"),
        ("import-dividends", cmd_import_dividends, "load dividend history from CSV"),
        ("import-macro", cmd_import_macro, "load macro series (CBR, T-bills, FX) from CSV"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("file", type=Path)
        if name == "import-prices":
            sp.add_argument("--ticker", help="ticker for single-stock files without a ticker column")
        sp.set_defaults(func=func)

    sub.add_parser("retag-news", help="re-run ticker/topic tagging on stored news").set_defaults(func=cmd_retag_news)
    commands.register(sub)
    sub.add_parser("daily", help="prices + news + alerts (schedule this after 15:00 EAT)").set_defaults(func=cmd_daily)
    sub.add_parser("status", help="row counts and last run of each job").set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except psycopg.OperationalError as exc:
        print(f"error: can't reach the database ({str(exc).splitlines()[0]}).\n"
              "  - No Docker? Set DATABASE_URL=embedded in your .env file (stores data in .pgdata/).\n"
              "  - Using Docker? Start Docker Desktop, then run `docker compose up -d`.",
              file=sys.stderr)
        return 3
    except (SourceError, csv_files.CsvFormatError, PortfolioError, LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
