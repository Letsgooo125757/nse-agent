"""Link news articles to NSE tickers and to macro topics.

Deliberately simple and explainable (phrase matching). Every link records
which alias matched so bad matches are easy to spot and fix by editing
aliases. A later phase can add an LLM pass on top for impact summaries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

# Topic -> trigger phrases (case-insensitive, word-bounded). These are the
# market-wide drivers the agent will later explain ("why did banks fall?").
TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "interest_rates": ("central bank rate", "CBR", "monetary policy committee", "MPC",
                       "interest rate", "rate cut", "rate hike", "treasury bill", "T-bill",
                       "bond yield", "Federal Reserve", "US Fed"),
    "currency": ("shilling", "exchange rate", "forex", "USD/KES", "dollar", "Eurobond"),
    "inflation": ("inflation", "consumer price index", "CPI", "cost of living"),
    "oil_energy": ("oil price", "crude", "Brent", "fuel price", "EPRA", "OPEC", "electricity tariff"),
    "tax_fiscal": ("Finance Bill", "Finance Act", "KRA", "excise duty", "tax", "budget", "National Treasury", "IMF"),
    "banking_regulation": ("core capital", "CBK", "Central Bank of Kenya", "Banking Act",
                           "non-performing loans", "NPL"),
    "capital_markets": ("Capital Markets Authority", "CMA", "Nairobi Securities Exchange",
                        "NSE", "IPO", "listing", "rights issue", "delisting"),
    "dividends_earnings": ("dividend", "profit after tax", "net profit", "earnings", "half-year results",
                           "full-year results", "profit warning", "book closure"),
    "global_markets": ("emerging markets", "frontier markets", "S&P 500", "global markets",
                       "recession", "tariffs"),
    "agriculture_weather": ("tea prices", "coffee prices", "drought", "El Niño", "La Niña", "rainfall"),
}


@dataclass(frozen=True)
class TickerMatch:
    ticker: str
    matched_alias: str
    in_title: bool
    mentions: int
    relevance: Decimal


def _pattern(phrase: str) -> re.Pattern[str]:
    # ALL-CAPS phrases (tickers/acronyms like KCB, CBK) are case-sensitive so
    # they don't fire on ordinary words; everything else is case-insensitive.
    # Custom boundaries so phrases with '&' or '-' (I&M, M-Pesa) still work.
    flags = 0 if (phrase.upper() == phrase and any(c.isalpha() for c in phrase)) else re.IGNORECASE
    return re.compile(rf"(?<![\w&-]){re.escape(phrase)}(?![\w&-])", flags)


class Tagger:
    def __init__(self, company_aliases: dict[str, list[str]]):
        # Longest aliases first so 'Kenya Power' is credited before 'KPLC' etc.
        self._companies = {
            ticker: sorted(((a, _pattern(a)) for a in aliases if a.strip()), key=lambda x: -len(x[0]))
            for ticker, aliases in company_aliases.items()
        }
        self._topics = {t: [_pattern(p) for p in phrases] for t, phrases in TOPIC_KEYWORDS.items()}

    def match_tickers(self, title: str, body: str | None) -> list[TickerMatch]:
        body = body or ""
        out: list[TickerMatch] = []
        for ticker, aliases in self._companies.items():
            best_alias, in_title, mentions = None, False, 0
            for alias, pat in aliases:
                t_hits = len(pat.findall(title))
                b_hits = len(pat.findall(body))
                if t_hits or b_hits:
                    best_alias = best_alias or alias
                    in_title = in_title or t_hits > 0
                    mentions += t_hits + b_hits
            if best_alias:
                out.append(TickerMatch(ticker, best_alias, in_title, mentions,
                                       self.relevance(in_title, mentions)))
        return sorted(out, key=lambda m: -m.relevance)

    @staticmethod
    def relevance(in_title: bool, mentions: int) -> Decimal:
        """0.9+ = the story is about this company; ~0.3 = passing mention."""
        score = (0.6 if in_title else 0.2) + min(mentions, 4) * 0.1
        return Decimal(str(round(min(score, 1.0), 2)))

    def match_topics(self, title: str, body: str | None) -> list[str]:
        text = f"{title}\n{body or ''}"
        return sorted(t for t, pats in self._topics.items() if any(p.search(text) for p in pats))
