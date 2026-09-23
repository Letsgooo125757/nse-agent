"""Write records to Postgres. Every write is an idempotent upsert, so any job
can be re-run safely (e.g. after a failure, or to backfill)."""
from __future__ import annotations

import csv
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterable, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg

from .config import DATA_DIR
from .models import IndexValue, MacroObservation, NewsItem, PriceBar
from .sources.rss import FeedConfig
from .tagging import Tagger

# ---------------------------------------------------------------- run log


@dataclass
class RunLog:
    id: int
    rows_seen: int = 0
    rows_written: int = 0
    warnings: list[str] = field(default_factory=list)
    status: str = "ok"


@contextmanager
def ingestion_run(conn: psycopg.Connection, job: str, source: str) -> Iterator[RunLog]:
    """Record a job in ingestion_runs. The run row is committed separately from
    the data so a failure is still logged after the data rolls back."""
    rid = conn.execute(
        "INSERT INTO ingestion_runs(job, source) VALUES (%s, %s) RETURNING id", (job, source)
    ).fetchone()["id"]
    conn.commit()
    log = RunLog(rid)
    try:
        yield log
        conn.commit()
        _finish(conn, log, "partial" if (log.warnings and log.status == "ok") else log.status, None)
    except Exception as exc:
        conn.rollback()
        _finish(conn, log, "failed", f"{type(exc).__name__}: {exc}")
        raise


def _finish(conn: psycopg.Connection, log: RunLog, status: str, error: str | None) -> None:
    conn.execute(
        """UPDATE ingestion_runs SET finished_at = now(), status = %s, rows_seen = %s,
               rows_written = %s, warnings = %s, error = %s WHERE id = %s""",
        (status, log.rows_seen, log.rows_written, log.warnings[:50], error, log.id),
    )
    conn.commit()


# ---------------------------------------------------------------- seed data


def _read_csv(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def seed_reference_data(conn: psycopg.Connection) -> dict[str, int]:
    sectors = _read_csv("sectors.csv")
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO sectors(sector_code, name) VALUES (%(sector_code)s, %(name)s)
               ON CONFLICT (sector_code) DO UPDATE SET name = EXCLUDED.name""",
            sectors,
        )
        companies = _read_csv("companies.csv")
        cur.executemany(
            """INSERT INTO companies(ticker, name, sector_code, market_segment, security_type,
                                     is_active, aliases)
               VALUES (%(ticker)s, %(name)s, %(sector_code)s, NULLIF(%(market_segment)s, ''),
                       %(security_type)s, %(is_active)s::boolean, %(aliases)s)
               ON CONFLICT (ticker) DO UPDATE SET
                   name = EXCLUDED.name, sector_code = EXCLUDED.sector_code,
                   market_segment = EXCLUDED.market_segment,
                   security_type = EXCLUDED.security_type,
                   is_active = EXCLUDED.is_active, aliases = EXCLUDED.aliases,
                   updated_at = now()""",
            [{**c, "aliases": [a.strip() for a in c["aliases"].split("|") if a.strip()]}
             for c in companies],
        )
        indices = [("NASI", "NSE All Share Index"), ("NSE20", "NSE 20 Share Index"),
                   ("NSE25", "NSE 25 Share Index"), ("NSE10", "NSE 10 Share Index")]
        cur.executemany(
            "INSERT INTO market_indices VALUES (%s, %s) ON CONFLICT (index_code) DO NOTHING", indices
        )
        macro = _read_csv("macro_series.csv")
        cur.executemany(
            """INSERT INTO macro_series VALUES (%(series_code)s, %(name)s, %(unit)s,
                                                %(frequency)s, %(source)s)
               ON CONFLICT (series_code) DO UPDATE SET name = EXCLUDED.name, unit = EXCLUDED.unit,
                   frequency = EXCLUDED.frequency, source = EXCLUDED.source""",
            macro,
        )
    return {"sectors": len(sectors), "companies": len(companies),
            "indices": len(indices), "macro_series": len(macro)}


def sync_news_sources(conn: psycopg.Connection, feeds: Iterable[FeedConfig]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO news_sources(source_code, name, feed_url, scope, enabled)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (source_code) DO UPDATE SET name = EXCLUDED.name,
                   feed_url = EXCLUDED.feed_url, scope = EXCLUDED.scope, enabled = EXCLUDED.enabled""",
            [(f.source_code, f.name, f.feed_url, f.scope, f.enabled) for f in feeds],
        )


# ---------------------------------------------------------------- prices


def known_tickers(conn: psycopg.Connection) -> set[str]:
    return {r["ticker"] for r in conn.execute("SELECT ticker FROM companies")}


def upsert_prices(conn: psycopg.Connection, bars: list[PriceBar], log: RunLog) -> None:
    """Unknown tickers are skipped with a warning (add them to companies.csv
    and re-run `seed`) rather than silently creating half-filled companies."""
    tickers = known_tickers(conn)
    good = [b for b in bars if b.ticker in tickers]
    unknown = sorted({b.ticker for b in bars} - tickers)
    if unknown:
        log.warnings.append(f"Skipped unknown tickers: {', '.join(unknown)}")
    log.rows_seen += len(bars)
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO daily_prices(ticker, trade_date, open, high, low, close, prev_close,
                                        volume, turnover, vwap, source)
               VALUES (%(ticker)s, %(trade_date)s, %(open)s, %(high)s, %(low)s, %(close)s,
                       %(prev_close)s, %(volume)s, %(turnover)s, %(vwap)s, %(source)s)
               ON CONFLICT (ticker, trade_date) DO UPDATE SET
                   -- keep richer data: never overwrite a known value with NULL
                   open       = COALESCE(EXCLUDED.open, daily_prices.open),
                   high       = COALESCE(EXCLUDED.high, daily_prices.high),
                   low        = COALESCE(EXCLUDED.low, daily_prices.low),
                   close      = EXCLUDED.close,
                   prev_close = COALESCE(EXCLUDED.prev_close, daily_prices.prev_close),
                   volume     = COALESCE(EXCLUDED.volume, daily_prices.volume),
                   turnover   = COALESCE(EXCLUDED.turnover, daily_prices.turnover),
                   vwap       = COALESCE(EXCLUDED.vwap, daily_prices.vwap),
                   source     = EXCLUDED.source,
                   ingested_at = now()""",
            [b.__dict__ for b in good],
        )
    log.rows_written += len(good)


def upsert_indices(conn: psycopg.Connection, values: list[IndexValue], log: RunLog) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO index_values(index_code, trade_date, value, source)
               VALUES (%(index_code)s, %(trade_date)s, %(value)s, %(source)s)
               ON CONFLICT (index_code, trade_date) DO UPDATE SET value = EXCLUDED.value,
                   source = EXCLUDED.source, ingested_at = now()""",
            [v.__dict__ for v in values],
        )
    log.rows_written += len(values)


# ---------------------------------------------------------------- fundamentals


def upsert_financials(conn: psycopg.Connection, rows: list[dict], log: RunLog) -> None:
    tickers = known_tickers(conn)
    good = [r for r in rows if r["ticker"] in tickers]
    if len(good) < len(rows):
        log.warnings.append("Skipped unknown tickers: " +
                            ", ".join(sorted({r["ticker"] for r in rows} - tickers)))
    log.rows_seen += len(rows)
    cols = ["ticker", "period_end", "period_type", "currency", "revenue", "operating_profit",
            "profit_before_tax", "net_income", "total_assets", "total_liabilities",
            "total_equity", "operating_cash_flow", "eps", "dps", "shares_outstanding", "source_url"]
    updates = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, financial_statements.{c})"
                        for c in cols[3:])
    with conn.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO financial_statements({', '.join(cols)})
                VALUES ({', '.join(f'%({c})s' for c in cols)})
                ON CONFLICT (ticker, period_end, period_type) DO UPDATE SET {updates},
                    ingested_at = now()""",
            good,
        )
    log.rows_written += len(good)


def upsert_dividends(conn: psycopg.Connection, rows: list[dict], log: RunLog) -> None:
    tickers = known_tickers(conn)
    good = [r for r in rows if r["ticker"] in tickers]
    if len(good) < len(rows):
        log.warnings.append("Skipped unknown tickers: " +
                            ", ".join(sorted({r["ticker"] for r in rows} - tickers)))
    log.rows_seen += len(rows)
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO corporate_actions(ticker, action_type, amount_per_share,
                   book_closure_date, payment_date, announced_date, financial_year, source_url)
               VALUES (%(ticker)s, %(action_type)s, %(amount_per_share)s, %(book_closure_date)s,
                       %(payment_date)s, %(announced_date)s, %(financial_year)s, %(source_url)s)
               ON CONFLICT (ticker, action_type, book_closure_date, financial_year) DO UPDATE SET
                   amount_per_share = EXCLUDED.amount_per_share,
                   payment_date = COALESCE(EXCLUDED.payment_date, corporate_actions.payment_date),
                   announced_date = COALESCE(EXCLUDED.announced_date, corporate_actions.announced_date),
                   source_url = COALESCE(EXCLUDED.source_url, corporate_actions.source_url)""",
            good,
        )
    log.rows_written += len(good)


# ---------------------------------------------------------------- macro


def upsert_macro(conn: psycopg.Connection, obs: list[MacroObservation], log: RunLog) -> None:
    series = {r["series_code"] for r in conn.execute("SELECT series_code FROM macro_series")}
    good = [o for o in obs if o.series_code in series]
    if len(good) < len(obs):
        log.warnings.append("Unknown series (add to macro_series.csv): " +
                            ", ".join(sorted({o.series_code for o in obs} - series)))
    log.rows_seen += len(obs)
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO macro_observations(series_code, obs_date, value)
               VALUES (%(series_code)s, %(obs_date)s, %(value)s)
               ON CONFLICT (series_code, obs_date) DO UPDATE SET value = EXCLUDED.value,
                   ingested_at = now()""",
            [o.__dict__ for o in good],
        )
    log.rows_written += len(good)


# ---------------------------------------------------------------- news

_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                    "fbclid", "gclid", "ref"}


def normalise_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _TRACKING_PARAMS))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalise_url(url).encode()).hexdigest()


def build_tagger(conn: psycopg.Connection) -> Tagger:
    rows = conn.execute("SELECT ticker, aliases FROM companies WHERE is_active").fetchall()
    return Tagger({r["ticker"]: list(r["aliases"]) for r in rows})


def upsert_news(conn: psycopg.Connection, items: list[NewsItem], tagger: Tagger, log: RunLog) -> int:
    """Insert new articles (existing URLs are left alone) and tag them.
    Returns the number of ticker links created."""
    links = 0
    log.rows_seen += len(items)
    for item in items:
        body = item.content or item.summary
        topics = tagger.match_topics(item.title, body)
        row = conn.execute(
            """INSERT INTO news_articles(source_code, url, url_hash, title, summary, content,
                                         author, categories, topics, published_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (url_hash) DO NOTHING
               RETURNING id""",
            (item.source_code, item.url, url_hash(item.url), item.title, item.summary,
             item.content, item.author, list(item.categories), topics, item.published_at),
        ).fetchone()
        if row is None:  # already stored
            continue
        log.rows_written += 1
        matches = tagger.match_tickers(item.title, body)
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO news_ticker_links(article_id, ticker, matched_alias, in_title,
                                                 mentions, relevance)
                   VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING""",
                [(row["id"], m.ticker, m.matched_alias, m.in_title, m.mentions, m.relevance)
                 for m in matches],
            )
        links += len(matches)
    return links


def retag_all_news(conn: psycopg.Connection, tagger: Tagger) -> int:
    """Re-run tagging over every stored article (after editing aliases)."""
    conn.execute("DELETE FROM news_ticker_links")
    n = 0
    for a in conn.execute("SELECT id, title, COALESCE(content, summary) AS body FROM news_articles").fetchall():
        conn.execute("UPDATE news_articles SET topics = %s WHERE id = %s",
                     (tagger.match_topics(a["title"], a["body"]), a["id"]))
        for m in tagger.match_tickers(a["title"], a["body"]):
            conn.execute(
                """INSERT INTO news_ticker_links VALUES (%s, %s, %s, %s, %s, %s)""",
                (a["id"], m.ticker, m.matched_alias, m.in_title, m.mentions, m.relevance),
            )
            n += 1
    return n
