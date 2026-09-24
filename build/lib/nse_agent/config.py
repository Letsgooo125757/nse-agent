"""Runtime settings, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / "data"
SCHEMA_PATH = PACKAGE_DIR.parent / "db" / "schema.sql"

NAIROBI_TZ = ZoneInfo("Africa/Nairobi")


@dataclass(frozen=True)
class Settings:
    database_url: str
    http_timeout: float
    user_agent: str
    afx_base_url: str
    # A price snapshot older than this (in calendar days) is flagged as stale.
    stale_after_days: int
    feeds_path: Path
    pgdata_dir: Path


def get_settings() -> Settings:
    return Settings(
        # "embedded" = a private Postgres stored in PGDATA_DIR, started on demand
        # (no Docker needed). Or a normal URL, e.g. postgresql://nse:nse@localhost:5432/nse
        database_url=os.getenv("DATABASE_URL", "embedded"),
        http_timeout=float(os.getenv("HTTP_TIMEOUT", "30")),
        user_agent=os.getenv(
            "HTTP_USER_AGENT",
            "nse-agent/0.1 (personal research project; contact: set HTTP_USER_AGENT)",
        ),
        afx_base_url=os.getenv("AFX_BASE_URL", "https://afx.kwayisi.org/nse/"),
        stale_after_days=int(os.getenv("STALE_AFTER_DAYS", "5")),
        feeds_path=Path(os.getenv("FEEDS_PATH", str(DATA_DIR / "feeds.json"))),
        pgdata_dir=Path(os.getenv("PGDATA_DIR", str(PACKAGE_DIR.parent / ".pgdata"))),
    )
