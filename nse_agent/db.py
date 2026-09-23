"""Database connection helpers."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row

from .config import SCHEMA_PATH, get_settings


@contextmanager
def connect(database_url: str | None = None) -> Iterator[psycopg.Connection]:
    """Open a connection; commit on success, roll back on error."""
    url = database_url or get_settings().database_url
    with psycopg.connect(url, row_factory=dict_row) as conn:
        yield conn


def apply_schema(conn: psycopg.Connection) -> None:
    """Create/upgrade all tables and views. Idempotent."""
    conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
