"""Copy every table from one database to another (e.g. local embedded -> hosted Neon).

    nse-agent db-copy --to "postgresql://...neon.tech/nse?sslmode=require"

The target gets the current schema first, then the data, in foreign-key order.
It refuses to overwrite a target that already has profiles or prices unless
--replace is given.
"""
from __future__ import annotations

import psycopg

from .db import apply_schema, resolve_url

# parents before children
TABLES = [
    "sectors", "companies", "market_indices", "macro_series", "news_sources",
    "daily_prices", "index_values", "corporate_actions", "financial_statements",
    "news_articles", "news_ticker_links", "macro_observations", "ingestion_runs",
    "investor_profiles", "transactions", "other_holdings", "allocation_targets",
    "watchlist", "price_alert_rules", "alerts", "app_usage",
]


def _columns(conn: psycopg.Connection, table: str) -> list[str]:
    rows = conn.execute(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (table,)).fetchall()
    return [r[0] for r in rows]


def copy_database(source_url: str, target_url: str, *, replace: bool = False,
                  log=print) -> dict[str, int]:
    counts: dict[str, int] = {}
    with psycopg.connect(resolve_url(source_url)) as src, psycopg.connect(target_url) as dst:
        apply_schema(dst)
        busy = dst.execute("""SELECT (SELECT count(*) FROM investor_profiles)
                                   + (SELECT count(*) FROM daily_prices)""").fetchone()[0]
        if busy and not replace:
            raise RuntimeError("The target database already has data. Re-run with --replace to overwrite it.")
        dst.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
        for table in TABLES:
            cols = [c for c in _columns(src, table) if c in set(_columns(dst, table))]
            col_sql = ", ".join(f'"{c}"' for c in cols)
            n = 0
            with src.cursor().copy(f'COPY {table} ({col_sql}) TO STDOUT') as out, \
                    dst.cursor().copy(f'COPY {table} ({col_sql}) FROM STDIN') as inp:
                for chunk in out:
                    inp.write(chunk)
            n = dst.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if "id" in cols:  # keep BIGSERIAL counters ahead of the copied ids
                dst.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                            f"GREATEST((SELECT max(id) FROM {table}), 1), (SELECT max(id) FROM {table}) IS NOT NULL)")
            counts[table] = n
            log(f"  {table:22} {n:>7} rows")
        dst.commit()
    return counts


def cmd_db_copy(args) -> int:
    if not args.to.startswith(("postgresql://", "postgres://")):
        print("error: --to must be a postgresql:// URL (copy it from your Neon dashboard).")
        return 2
    print("Copying your data to the target database...")
    try:
        counts = copy_database(args.source or "", args.to, replace=args.replace)
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 2
    print(f"Done: {sum(counts.values()):,} rows in {len(counts)} tables.")
    return 0


def register(sub) -> None:
    p = sub.add_parser("db-copy", help="copy all data to another database (e.g. your hosted one)")
    p.add_argument("--to", required=True, help="target postgresql:// URL")
    p.add_argument("--source", help="source URL (default: DATABASE_URL, e.g. your embedded database)")
    p.add_argument("--replace", action="store_true", help="overwrite data already in the target")
    p.set_defaults(func=cmd_db_copy)
