from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from nse_agent.cli import price_warnings
from nse_agent.config import NAIROBI_TZ
from nse_agent.loaders import normalise_url
from nse_agent.sources import afx, csv_files, rss
from nse_agent.sources.base import SourceError, parse_date, parse_decimal
from nse_agent.tagging import Tagger

from .conftest import FIXTURES


@pytest.mark.parametrize("text,expected", [
    ("1,234.50", Decimal("1234.50")), ("+0.10", Decimal("0.10")), ("-1.60", Decimal("-1.60")),
    ("40.1B", Decimal("40100000000")), ("KES 1.77Tr", Decimal("1770000000000")),
    ("-3.98%", Decimal("-3.98")), ("", None), ("-", None), (None, None), ("abc", None),
])
def test_parse_decimal(text, expected):
    assert parse_decimal(text) == expected


@pytest.mark.parametrize("text", ["2024-11-20", "20/11/2024", "20-Nov-2024", "November 20, 2024"])
def test_parse_date(text):
    assert parse_date(text) == date(2024, 11, 20)


def test_afx_market_page():
    batch = afx.parse_market_page((FIXTURES / "afx_market.html").read_text(), fallback_date=date(2030, 1, 1))
    assert batch.as_of == date(2024, 11, 20)
    assert not batch.warnings
    bars = {b.ticker: b for b in batch.bars}
    assert len(bars) == 10

    kcb = bars["KCB"]
    assert (kcb.close, kcb.volume, kcb.prev_close) == (Decimal("37.55"), 13000, Decimal("39.15"))
    # Untraded: volume 0, price carried forward
    assert bars["BAMB"].volume == 0 and bars["BAMB"].prev_close == Decimal("66.00")
    assert bars["GLD"].close == Decimal("3220.00")
    assert bars["KPLC-P4"].close == Decimal("4.00")

    assert [(i.index_code, i.value) for i in batch.indices] == [("NASI", Decimal("112.71"))]


def test_afx_market_page_without_date_falls_back_with_warning():
    html = (FIXTURES / "afx_market.html").read_text().replace("TRADING SUMMARY FOR", "SUMMARY")
    batch = afx.parse_market_page(html, fallback_date=date(2030, 1, 1))
    assert batch.as_of == date(2030, 1, 1)
    assert batch.warnings


def test_afx_layout_change_raises():
    with pytest.raises(SourceError):
        afx.parse_market_page("<html><table><tr><th>Foo</th></tr></table></html>", fallback_date=date.today())


def test_afx_stock_page():
    bars = afx.parse_stock_page((FIXTURES / "afx_scom.html").read_text(), "scom")
    assert len(bars) == 5
    first = bars[0]
    assert (first.ticker, first.trade_date, first.close, first.volume) == \
        ("SCOM", date(2024, 11, 7), Decimal("15.70"), 10478000)
    assert first.prev_close == Decimal("16.35")  # 15.70 - (-0.65)


def test_rss_parse():
    items = rss.parse_feed((FIXTURES / "capitalfm.xml").read_bytes(), "capitalfm_business")
    assert len(items) == 3
    a = items[0]
    assert a.title.startswith("CBK okays")
    assert a.published_at == datetime(2026, 9, 23, 12, 15, 11, tzinfo=timezone.utc)
    assert "KCB Group" in a.content and "<p>" not in a.content
    assert "Banks" in a.categories


def test_rss_invalid():
    with pytest.raises(SourceError):
        rss.parse_feed(b"<html>not a feed", "x")


def test_prices_csv():
    res = csv_files.read_prices_csv(FIXTURES / "prices_history.csv")
    assert len(res.rows) == 4               # NOPE parses; unknown tickers are the loader's job
    assert len(res.skipped) == 1            # bad-date
    kcb = next(b for b in res.rows if b.ticker == "KCB")
    assert kcb.trade_date == date(2024, 11, 19) and kcb.volume == 120000
    assert res.rows[0].volume == 3_100_000


def test_prices_csv_missing_close(tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("date,ticker,foo\n2024-01-01,SCOM,1\n")
    with pytest.raises(csv_files.CsvFormatError):
        csv_files.read_prices_csv(p)


def test_dividends_csv_rejects_non_dividend():
    res = csv_files.read_dividends_csv(FIXTURES / "dividends.csv")
    assert len(res.rows) == 2 and len(res.skipped) == 1


def test_normalise_url():
    assert normalise_url("https://Capitalfm.africa/story/?utm_source=rss&b=2&a=1") == \
        "https://capitalfm.africa/story?a=1&b=2"


# ---------------------------------------------------------------- tagging

TAGGER = Tagger({
    "KCB": ["KCB Group", "KCB Bank", "KCB"],
    "SCBK": ["Standard Chartered", "StanChart"],
    "SGL": ["Standard Group"],
    "SCOM": ["Safaricom", "M-Pesa"],
    "IMH": ["I&M Bank", "I&M Group"],
    "EQTY": ["Equity Group", "Equity Bank"],
})


def test_tagger_title_match_is_high_relevance():
    m = TAGGER.match_tickers("StanChart falls to tier two bank", "Standard Chartered Bank Kenya has moved...")
    assert m[0].ticker == "SCBK" and m[0].in_title and m[0].relevance >= Decimal("0.8")


def test_tagger_ignores_lowercase_ticker_and_substrings():
    # 'kcb' in lowercase and 'Equity' alone shouldn't match; 'Standard' alone isn't an alias
    m = TAGGER.match_tickers("Private equity firms eye Standard gauge railway", "see kcb-news.com")
    assert m == []


def test_tagger_special_characters():
    tickers = {m.ticker for m in TAGGER.match_tickers("I&M Bank and M-Pesa partnership", None)}
    assert tickers == {"IMH", "SCOM"}


def test_topics():
    topics = TAGGER.match_topics("CBK cuts central bank rate", "The shilling held; inflation eased.")
    assert {"interest_rates", "banking_regulation", "currency", "inflation"} <= set(topics)


# ---------------------------------------------------------------- freshness


def test_price_warnings():
    fri_noon = datetime(2026, 9, 25, 12, 0, tzinfo=NAIROBI_TZ)
    assert "trading hours" in price_warnings(fri_noon.date(), fri_noon, 5)[0]
    fri_eve = fri_noon.replace(hour=17)
    assert price_warnings(fri_eve.date(), fri_eve, 5) == []
    assert "Stale" in price_warnings(date(2024, 11, 20), fri_eve, 5)[0]
