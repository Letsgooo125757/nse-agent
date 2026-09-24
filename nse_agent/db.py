"""Database connection helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import SCHEMA_PATH, get_settings

_embedded = None  # the running embedded server, kept for the life of the process


def resolve_url(database_url: str | None = None) -> str:
    """Turn DATABASE_URL into a real connection string.

    `embedded` starts (or reuses) a private PostgreSQL whose files live in
    PGDATA_DIR (default: .pgdata/ in the project). Data persists between
    runs; the server stops when the command finishes.
    """
    global _embedded
    s = get_settings()
    url = database_url or s.database_url
    if url.strip().lower() != "embedded":
        return url
    if _embedded is None:
        try:
            import pgserver
        except ImportError as exc:
            raise RuntimeError("DATABASE_URL=embedded needs the pgserver package: "
                               "pip install pgserver") from exc
        s.pgdata_dir.mkdir(parents=True, exist_ok=True)
        _embedded = pgserver.get_server(s.pgdata_dir, cleanup_mode="stop")
    return _embedded.get_uri()


@contextmanager
def connect(database_url: str | None = None) -> Iterator[psycopg.Connection]:
    """Open a connection; commit on success, roll back on error."""
    with psycopg.connect(resolve_url(database_url), row_factory=dict_row) as conn:
        yield conn


def apply_schema(conn: psycopg.Connection) -> None:
    """Create/upgrade all tables and views. Idempotent."""
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
