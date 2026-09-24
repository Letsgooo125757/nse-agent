"""Alerts: turn the day's data into a short list of things worth your attention.

Each check returns AlertDraft objects. `run_alerts` stores them with a
dedupe key, so running it several times a day never repeats an alert.
Weekly checks (concentration, drift) put the ISO week in the key, so they
come back at most once a week while the condition lasts.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

import psycopg

from .fees import dividend_tax
from .investor import load_profile
from .planner import DRIFT_TOLERANCE_PP, INVEST_CLASSES, LABELS, build_plan
from .portfolio import PortfolioSummary, fmt_qty, snapshot

BIG_MOVE_PCT = Decimal("5")
BOOK_CLOSURE_DAYS = 14
NEWS_MIN_RELEVANCE = Decimal("0.8")
NEWS_LOOKBACK = timedelta(days=2)


@dataclass
class AlertDraft:
    alert_type: str
    severity: str           # info | warning | action
    message: str
    dedupe_key: str
    ticker: str | None = None
    details: dict = field(default_factory=dict)


def _week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


# ---------------------------------------------------------------- checks


def check_price_rules(rules: list[dict], prices: dict[str, dict]) -> list[AlertDraft]:
    out = []
    for r in rules:
        px = prices.get(r["ticker"])
        if not px:
            continue
        close, thr, d = px["close"], r["threshold"], px["trade_date"]
        hit = ((r["condition"] == "above" and close >= thr)
               or (r["condition"] == "below" and close <= thr)
               or (r["condition"] == "move_pct" and px["change_pct"] is not None
                   and abs(px["change_pct"]) >= thr))
        if hit:
            what = (f"moved {px['change_pct']:+}%" if r["condition"] == "move_pct"
                    else f"is {r['condition']} your level of KES {thr:.2f}")
            out.append(AlertDraft("price_rule", "action",
                                  f"{r['ticker']} {what} (close KES {close:.2f} on {d}).",
                                  f"rule:{r['id']}:{d}", r["ticker"],
                                  {"rule_id": r["id"], "close": str(close)}))
    return out


def check_moves(tickers: set[str], prices: dict[str, dict], held: set[str]) -> list[AlertDraft]:
    out = []
    for t in sorted(tickers):
        px = prices.get(t)
        if not px:
            continue
        d, chg = px["trade_date"], px["change_pct"]
        tag = "you hold" if t in held else "on your watchlist"
        if chg is not None and abs(chg) >= BIG_MOVE_PCT and (px["volume"] or 0) > 0:
            out.append(AlertDraft("big_move", "warning",
                                  f"{t} ({tag}) moved {chg:+}% to KES {px['close']:.2f} on {d}. "
                                  "Check the news before reacting.", f"move:{t}:{d}", t,
                                  {"change_pct": str(chg)}))
        enough_history = (px.get("trading_days_52w") or 0) >= 100
        if enough_history and px["high_52w"] is not None and px["close"] >= px["high_52w"]:
            out.append(AlertDraft("52w_high", "info", f"{t} ({tag}) closed at a 52-week high of KES {px['close']:.2f}.",
                                  f"52wh:{t}:{d}", t))
        elif enough_history and px["low_52w"] is not None and px["close"] <= px["low_52w"]:
            out.append(AlertDraft("52w_low", "info", f"{t} ({tag}) closed at a 52-week low of KES {px['close']:.2f}.",
                                  f"52wl:{t}:{d}", t))
    return out


def check_book_closures(actions: list[dict], held_shares: dict[str, Decimal], today: date) -> list[AlertDraft]:
    out = []
    for a in actions:
        days = (a["book_closure_date"] - today).days
        if not 0 <= days <= BOOK_CLOSURE_DAYS:
            continue
        t, dps = a["ticker"], a["amount_per_share"]
        shares = held_shares.get(t, Decimal(0))
        kind = a["action_type"].replace("_", " ")
        if shares > 0:
            gross = shares * dps
            msg = (f"{t} {kind} of KES {dps}/share: books close {a['book_closure_date']} (in {days} days). "
                   f"On your {fmt_qty(shares)} shares that's about KES {gross:,.2f} gross, "
                   f"KES {gross - dividend_tax(gross):,.2f} after 5% withholding tax.")
            sev = "action"
        else:
            msg = (f"{t} (watchlist) {kind} of KES {dps}/share: books close {a['book_closure_date']} "
                   f"(in {days} days). You must own shares before that date to qualify.")
            sev = "info"
        out.append(AlertDraft("book_closure", sev, msg, f"bc:{a['id']}", t,
                              {"payment_date": str(a["payment_date"])}))
    return out


def check_concentration(s: PortfolioSummary, max_stock: Decimal, max_sector: Decimal,
                        today: date) -> list[AlertDraft]:
    out, wk = [], _week(today)
    if not s.holdings:
        return out
    # With a 20% cap you need at least 5 companies to comply at all, so below
    # that we send one diversification nudge instead of a limit breach per stock.
    needed = math.ceil(100 / max_stock) if max_stock > 0 else 1
    if len(s.holdings) < needed:
        top = s.holdings[0]
        out.append(AlertDraft("diversification", "warning",
                              f"You hold {len(s.holdings)} compan{'y' if len(s.holdings) == 1 else 'ies'}; "
                              f"the largest, {top.ticker}, is {top.weight_pct}% of your shares. A "
                              f"{max_stock:.0f}% per-company limit needs at least {needed} companies. "
                              "Add new ones (from different sectors) as you invest more.",
                              f"div:{wk}", top.ticker))
        return out
    for h in s.holdings:
        if h.weight_pct > max_stock:
            out.append(AlertDraft("concentration", "warning",
                                  f"{h.ticker} is {h.weight_pct}% of your shares (limit {max_stock:.0f}%). "
                                  "Direct new money elsewhere rather than selling (selling costs fees).",
                                  f"conc:{h.ticker}:{wk}", h.ticker))
    for sector, pct in s.sector_pct.items():
        if pct > max_sector:
            out.append(AlertDraft("sector_concentration", "warning",
                                  f"{sector.title()} is {pct}% of your shares (limit {max_sector:.0f}%).",
                                  f"sector:{sector}:{wk}", None, {"sector": sector}))
    return out


def check_plan(profile, s: PortfolioSummary, today: date) -> list[AlertDraft]:
    out, wk = [], _week(today)
    plan = build_plan(profile, s.class_values())
    if plan.emergency_target and plan.emergency_current < plan.emergency_target:
        out.append(AlertDraft("emergency_fund", "info",
                              f"Emergency fund is KES {plan.emergency_current:,.0f} of the "
                              f"KES {plan.emergency_target:,.0f} target.",
                              f"ef:{today:%Y-%m}"))
    off = [c for c in INVEST_CLASSES
           if plan.drift_pp.get(c) is not None and abs(plan.drift_pp[c]) > DRIFT_TOLERANCE_PP]
    if off:
        parts = [f"{LABELS[c]} {plan.current_pct[c]}% (target {plan.targets[c]}%)" for c in off]
        out.append(AlertDraft("drift", "info",
                              "Your mix has drifted from target: " + "; ".join(parts) +
                              ". `nse-agent plan` routes new money to fix this.",
                              f"drift:{wk}", None, {"classes": off}))
    return out


def check_stale(latest: date | None, today: date, stale_after_days: int) -> list[AlertDraft]:
    if latest is None:
        return [AlertDraft("stale_data", "warning", "No prices in the database yet. Run `nse-agent ingest-prices`.",
                           f"stale:none:{today}")]
    if (today - latest).days > stale_after_days:
        return [AlertDraft("stale_data", "warning",
                           f"Latest prices are from {latest}. Alerts based on prices may be out of date.",
                           f"stale:{latest}")]
    return []


def check_news(articles: list[dict]) -> list[AlertDraft]:
    return [AlertDraft("news", "info", f"{a['ticker']} in the news: {a['title']} ({a['source_code']})",
                       f"news:{a['article_id']}:{a['ticker']}", a["ticker"], {"url": a["url"]})
            for a in articles]


# ---------------------------------------------------------------- runner


def run_alerts(conn: psycopg.Connection, profile_id: int, *, today: date,
               stale_after_days: int = 5) -> list[dict]:
    """Evaluate every check, store new alerts, return the ones just created."""
    profile = load_profile(conn, profile_id)
    s = snapshot(conn, profile_id)
    held = {h.ticker: h.shares for h in s.holdings}
    watch = {r["ticker"] for r in conn.execute(
        "SELECT ticker FROM watchlist WHERE profile_id = %s", (profile_id,))}
    tracked = set(held) | watch
    prices = {r["ticker"]: r for r in conn.execute(
        "SELECT * FROM v_latest_prices WHERE ticker = ANY(%s)", (list(tracked),))} if tracked else {}
    rules = conn.execute("SELECT * FROM price_alert_rules WHERE profile_id = %s AND active",
                         (profile_id,)).fetchall()
    rule_prices = {r["ticker"]: r for r in conn.execute(
        "SELECT * FROM v_latest_prices WHERE ticker = ANY(%s)", ([r["ticker"] for r in rules],))} if rules else {}
    actions = conn.execute(
        """SELECT * FROM corporate_actions WHERE ticker = ANY(%s) AND action_type LIKE '%%dividend'
           AND book_closure_date BETWEEN %s AND %s""",
        (list(tracked), today, today + timedelta(days=BOOK_CLOSURE_DAYS))).fetchall() if tracked else []
    news = conn.execute(
        """SELECT * FROM v_ticker_news WHERE ticker = ANY(%s) AND relevance >= %s
           AND COALESCE(published_at, now()) >= %s""",
        (list(tracked), NEWS_MIN_RELEVANCE, today - NEWS_LOOKBACK)).fetchall() if tracked else []
    latest = conn.execute("SELECT max(trade_date) AS d FROM daily_prices").fetchone()["d"]

    drafts = (check_stale(latest, today, stale_after_days)
              + check_price_rules(rules, rule_prices)
              + check_moves(tracked, prices, set(held))
              + check_book_closures(actions, held, today)
              + check_concentration(s, profile.max_stock_pct, profile.max_sector_pct, today)
              + check_plan(profile, s, today)
              + check_news(news))

    created = []
    for a in drafts:
        row = conn.execute(
            """INSERT INTO alerts(profile_id, alert_type, severity, ticker, message, details, dedupe_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (profile_id, dedupe_key) DO NOTHING RETURNING *""",
            (profile_id, a.alert_type, a.severity, a.ticker, a.message, json.dumps(a.details), a.dedupe_key),
        ).fetchone()
        if row:
            created.append(row)
    return created
