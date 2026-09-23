"""RSS/Atom news adapter."""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import feedparser
from bs4 import BeautifulSoup

from ..models import NewsItem
from .base import SourceError, fetch


@dataclass(frozen=True)
class FeedConfig:
    source_code: str
    name: str
    feed_url: str
    scope: str
    enabled: bool = True
    note: str = ""


def load_feeds(path: Path) -> list[FeedConfig]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [FeedConfig(**{k: v for k, v in f.items() if k in FeedConfig.__annotations__}) for f in raw]


def html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    return text or None


def _published(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(calendar.timegm(st), tz=timezone.utc)
    return None


def parse_feed(data: bytes | str, source_code: str) -> list[NewsItem]:
    parsed = feedparser.parse(data)
    if parsed.bozo and not parsed.entries:
        raise SourceError(f"{source_code}: not a valid feed ({parsed.bozo_exception})")
    items: list[NewsItem] = []
    for e in parsed.entries:
        url = e.get("link")
        title = html_to_text(e.get("title"))
        if not url or not title:
            continue
        content_html = None
        if e.get("content"):
            content_html = e["content"][0].get("value")
        items.append(NewsItem(
            source_code=source_code,
            url=url.strip(),
            title=title,
            summary=html_to_text(e.get("summary")),
            content=html_to_text(content_html),
            author=e.get("author"),
            categories=tuple(t.get("term") for t in e.get("tags", []) if t.get("term")),
            published_at=_published(e),
        ))
    return items


def fetch_feed(feed: FeedConfig) -> list[NewsItem]:
    return parse_feed(fetch(feed.feed_url), feed.source_code)
