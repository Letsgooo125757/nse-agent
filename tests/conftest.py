"""Test fixtures.

Integration tests need Postgres. They use, in order:
  1. TEST_DATABASE_URL, if set (e.g. the docker-compose DB — tests DROP and
     recreate the public schema there, so don't point it at real data), or
  2. an embedded throwaway Postgres from the `pgserver` package, or
  3. are skipped.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def database_url():
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        yield url
        return
    try:
        import pgserver
    except ImportError:
        pytest.skip("No TEST_DATABASE_URL and pgserver not installed")
    tmp = tempfile.mkdtemp(prefix="nse_pg_")
    srv = pgserver.get_server(tmp, cleanup_mode="delete")
    yield srv.get_uri()
    srv.cleanup()


@pytest.fixture()
def db(database_url, monkeypatch):
    """A freshly initialised, seeded database per test."""
    import psycopg

    from nse_agent import loaders
    from nse_agent.db import apply_schema, connect

    with psycopg.connect(database_url, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    monkeypatch.setenv("DATABASE_URL", database_url)
    with connect(database_url) as conn:
        apply_schema(conn)
        loaders.seed_reference_data(conn)
    return database_url
