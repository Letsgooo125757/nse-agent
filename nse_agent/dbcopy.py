"""Copy every table from one database to another (e.g. local embedded -> hosted Neon).

    nse-agent db-copy --to "postgresql://...neon.tech/nse?sslmode=require"

The target gets the current schema first, then the data, in foreign-key order.
It refuses to overwrite a target that already has profiles or prices unless
--replace is given.
"""
from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from .db import apply_schema, resolve_url

# parents before children
TABLES = [
    "sectors", "companies", "market_indices", "macro_series", "news_sources",
    "daily_prices", "index_values", "corporate_actions", "financial_statements",
    "news_articles", "news_ticker_links", "article_analysis", "macro_observations", "ingestion_runs",
    "investor_profiles", "transactions", "other_holdings", "allocation_targets",
    "watchlist", "price_alert_rules", "alerts", "app_usage",
]


def _columns(conn: psycopg.Connection, table: str) -> list[tuple[str, str]]:
    rows = conn.execute(
        """SELECT column_name, data_type FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position""", (table,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def _same_database(a: psycopg.Connection, b: psycopg.Connection) -> bool:
    """True if both connections reach the same database (works through poolers)."""
    probe = "SELECT current_database(), (SELECT system_identifier FROM pg_control_system())"
    try:
        return a.execute(probe).fetchone() == b.execute(probe).fetchone()
    except psycopg.Error:
        a.rollback(); b.rollback()
        q = "SELECT current_database(), inet_server_addr()::text, inet_server_port()"
        return a.execute(q).fetchone() == b.execute(q).fetchone()


def copy_database(source_url: str, target_url: str, *, replace: bool = False,
                  log=print, batch: int = 1000) -> dict[str, int]:
    """Copy all rows table by table with plain SELECT/INSERT.

    Plain statements (not COPY) work through connection poolers such as
    Neon's; prepare_threshold=None avoids prepared statements for the same reason.
    """
    counts: dict[str, int] = {}
    with psycopg.connect(resolve_url(source_url), prepare_threshold=None) as src, \
            psycopg.connect(target_url, prepare_threshold=None) as dst:
        if _same_database(src, dst):
            raise RuntimeError("The source and target are the same database. The source should be your "
                               "local one: run `nse-agent db-copy --to ...` without --source "
                               "(it copies from the embedded database in .pgdata/).")
        src.rollback(); dst.rollback()
        apply_schema(src)   # bring an older local database up to date first (idempotent)
        src.commit()
        apply_schema(dst)
        busy = dst.execute("""SELECT (SELECT count(*) FROM investor_profiles)
                                   + (SELECT count(*) FROM daily_prices)""").fetchone()[0]
        if busy and not replace:
            raise RuntimeError("The target database already has data. Re-run with --replace to overwrite it.")
        dst.execute("TRUNCATE " + ", ".join(TABLES) + " RESTART IDENTITY CASCADE")
        for table in TABLES:
            target_cols = dict(_columns(dst, table))
            cols = [(c, t) for c, t in _columns(src, table) if c in target_cols]
            if not cols:
                counts[table] = 0
                continue
            names = [c for c, _ in cols]
            json_idx = [i for i, (_, t) in enumerate(cols) if t in ("json", "jsonb")]
            col_sql = ", ".join(f'"{c}"' for c in names)
            insert = f'INSERT INTO {table} ({col_sql}) VALUES ({", ".join(["%s"] * len(names))})'
            n = 0
            with src.cursor() as rd:
                rd.execute(f"SELECT {col_sql} FROM {table}")
                while rows := rd.fetchmany(batch):
                    if json_idx:
                        rows = [tuple(Jsonb(v) if i in json_idx and v is not None else v
                                      for i, v in enumerate(r)) for r in rows]
                    with dst.cursor() as wr:
                        wr.executemany(insert, rows)
                    n += len(rows)
            if "id" in names:  # keep BIGSERIAL counters ahead of the copied ids
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
        counts = copy_database(args.source, args.to, replace=args.replace)
    except RuntimeError as exc:
        print(f"error: {exc}")
        return 2
    print(f"Done: {sum(counts.values()):,} rows in {len(counts)} tables.")
    return 0


def register(sub) -> None:
    p = sub.add_parser("db-copy", help="copy all data to another database (e.g. your hosted one)")
    p.add_argument("--to", required=True, help="target postgresql:// URL")
    p.add_argument("--source", default="embedded",
                   help="source database (default: your local embedded one in .pgdata/)")
    p.add_argument("--replace", action="store_true", help="overwrite data already in the target")
    p.set_defaults(func=cmd_db_copy)
