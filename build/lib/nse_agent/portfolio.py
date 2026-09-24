"""Portfolio tracker: holdings, cost basis, P&L, dividends and exposure.

Uses the average-cost method (what Kenyan brokers and the CDSC statement
effectively show). Transaction sides:

  buy       quantity x price, fees added to cost
  sell      realised P&L = proceeds - fees - average cost of shares sold
  dividend  quantity = shares entitled, price = dividend per share, tax = WHT
  bonus     quantity = new shares received, price 0 (cost unchanged)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Iterable

import psycopg

from .fees import trade_fees

ZERO = Decimal(0)


@dataclass(frozen=True)
class Txn:
    ticker: str
    side: str
    trade_date: date
    quantity: Decimal
    price: Decimal
    fees: Decimal = ZERO
    tax: Decimal = ZERO
    id: int | None = None


@dataclass
class Position:
    ticker: str
    shares: Decimal = ZERO
    cost: Decimal = ZERO                 # remaining cost basis incl. buy fees
    realised_pnl: Decimal = ZERO
    dividends_gross: Decimal = ZERO
    dividends_tax: Decimal = ZERO
    fees_paid: Decimal = ZERO
    first_bought: date | None = None

    @property
    def avg_cost(self) -> Decimal | None:
        return (self.cost / self.shares) if self.shares else None

    @property
    def dividends_net(self) -> Decimal:
        return self.dividends_gross - self.dividends_tax


class PortfolioError(ValueError):
    pass


def fmt_qty(x: Decimal) -> str:
    """Decimal('1000.0000') -> '1,000'; Decimal('10.5') -> '10.5'."""
    return f"{Decimal(x).normalize():,f}"


def build_positions(txns: Iterable[Txn]) -> dict[str, Position]:
    """Replay transactions in date order (buys before sells on the same day)."""
    order = {"bonus": 0, "buy": 1, "dividend": 2, "sell": 3}
    positions: dict[str, Position] = {}
    for t in sorted(txns, key=lambda t: (t.trade_date, order[t.side], t.id or 0)):
        pos = positions.setdefault(t.ticker, Position(t.ticker))
        if t.side == "buy":
            pos.shares += t.quantity
            pos.cost += t.quantity * t.price + t.fees
            pos.fees_paid += t.fees
            pos.first_bought = pos.first_bought or t.trade_date
        elif t.side == "sell":
            if t.quantity > pos.shares:
                raise PortfolioError(
                    f"{t.ticker}: selling {fmt_qty(t.quantity)} on {t.trade_date} but only "
                    f"{fmt_qty(pos.shares)} held")
            avg = pos.cost / pos.shares
            pos.realised_pnl += t.quantity * t.price - t.fees - avg * t.quantity
            pos.cost -= avg * t.quantity
            pos.shares -= t.quantity
            pos.fees_paid += t.fees
            if pos.shares == 0:
                pos.cost = ZERO  # clear rounding dust
        elif t.side == "bonus":
            pos.shares += t.quantity
        elif t.side == "dividend":
            pos.dividends_gross += t.quantity * t.price
            pos.dividends_tax += t.tax
        else:
            raise PortfolioError(f"Unknown side {t.side!r}")
    return positions


def shares_held_on(txns: Iterable[Txn], ticker: str, on: date) -> Decimal:
    held = ZERO
    for t in txns:
        if t.ticker != ticker or t.trade_date > on:
            continue
        if t.side in ("buy", "bonus"):
            held += t.quantity
        elif t.side == "sell":
            held -= t.quantity
    return held


@dataclass
class HoldingView:
    ticker: str
    name: str
    sector: str | None
    shares: Decimal
    avg_cost: Decimal
    cost: Decimal
    price: Decimal | None
    price_date: date | None
    market_value: Decimal
    unrealised_pnl: Decimal
    unrealised_pct: Decimal | None
    exit_fees: Decimal                  # what selling everything today would cost
    dividends_net: Decimal
    realised_pnl: Decimal
    weight_pct: Decimal = ZERO          # of the share portfolio


@dataclass
class PortfolioSummary:
    holdings: list[HoldingView]
    closed: list[Position]              # fully sold, kept for realised P&L
    sector_pct: dict[str, Decimal]
    equity_value: Decimal
    equity_cost: Decimal
    unrealised_pnl: Decimal
    realised_pnl: Decimal
    dividends_net: Decimal
    other: dict[str, Decimal] = field(default_factory=dict)   # asset class -> KES
    warnings: list[str] = field(default_factory=list)

    @property
    def total_return(self) -> Decimal:
        return self.unrealised_pnl + self.realised_pnl + self.dividends_net

    def class_values(self) -> dict[str, Decimal]:
        out = dict(self.other)
        out["nse_equities"] = self.equity_value
        return out


def summarise(positions: dict[str, Position], prices: dict[str, dict],
              other: dict[str, Decimal] | None = None) -> PortfolioSummary:
    """`prices` maps ticker -> row from v_latest_prices (close, trade_date, name, sector_code)."""
    holdings, closed, warnings = [], [], []
    for pos in positions.values():
        if pos.shares <= 0:
            closed.append(pos)
            continue
        px = prices.get(pos.ticker)
        if px is None or px.get("close") is None:
            warnings.append(f"{pos.ticker}: no price in the database. Valued at cost.")
            price, pdate, mv = None, None, pos.cost
        else:
            price, pdate = Decimal(px["close"]), px["trade_date"]
            mv = pos.shares * price
        unreal = mv - pos.cost
        holdings.append(HoldingView(
            ticker=pos.ticker, name=(px or {}).get("name", pos.ticker),
            sector=(px or {}).get("sector_code"), shares=pos.shares, avg_cost=pos.avg_cost,
            cost=pos.cost, price=price, price_date=pdate, market_value=mv, unrealised_pnl=unreal,
            unrealised_pct=(100 * unreal / pos.cost).quantize(Decimal("0.01")) if pos.cost else None,
            exit_fees=trade_fees(mv).total, dividends_net=pos.dividends_net,
            realised_pnl=pos.realised_pnl,
        ))
    equity_value = sum((h.market_value for h in holdings), ZERO)
    sectors: dict[str, Decimal] = {}
    for h in holdings:
        h.weight_pct = (100 * h.market_value / equity_value).quantize(Decimal("0.1")) if equity_value else ZERO
        key = h.sector or "UNKNOWN"
        sectors[key] = sectors.get(key, ZERO) + h.market_value
    sector_pct = {k: (100 * v / equity_value).quantize(Decimal("0.1")) for k, v in sectors.items()} \
        if equity_value else {}
    holdings.sort(key=lambda h: -h.market_value)
    all_pos = list(positions.values())
    return PortfolioSummary(
        holdings=holdings, closed=closed,
        sector_pct=dict(sorted(sector_pct.items(), key=lambda kv: -kv[1])),
        equity_value=equity_value, equity_cost=sum((h.cost for h in holdings), ZERO),
        unrealised_pnl=sum((h.unrealised_pnl for h in holdings), ZERO),
        realised_pnl=sum((p.realised_pnl for p in all_pos), ZERO),
        dividends_net=sum((p.dividends_net for p in all_pos), ZERO),
        other=other or {}, warnings=warnings,
    )


# ---------------------------------------------------------------- database


def load_txns(conn: psycopg.Connection, profile_id: int) -> list[Txn]:
    rows = conn.execute(
        "SELECT id, ticker, side, trade_date, quantity, price, fees, tax FROM transactions "
        "WHERE profile_id = %s ORDER BY trade_date, id", (profile_id,)).fetchall()
    return [Txn(**r) for r in rows]


def add_txn(conn: psycopg.Connection, profile_id: int, t: Txn, notes: str | None = None) -> int:
    # Validate against history first: e.g. can't sell shares you don't own.
    build_positions([*load_txns(conn, profile_id), t])
    return conn.execute(
        """INSERT INTO transactions(profile_id, ticker, side, trade_date, quantity, price, fees, tax, notes)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (profile_id, t.ticker, t.side, t.trade_date, t.quantity, t.price, t.fees, t.tax, notes),
    ).fetchone()["id"]


def load_other(conn: psycopg.Connection, profile_id: int) -> dict[str, Decimal]:
    rows = conn.execute(
        "SELECT asset_class, sum(amount) AS amount FROM other_holdings WHERE profile_id = %s "
        "GROUP BY asset_class", (profile_id,)).fetchall()
    return {r["asset_class"]: r["amount"] for r in rows}


def snapshot(conn: psycopg.Connection, profile_id: int) -> PortfolioSummary:
    positions = build_positions(load_txns(conn, profile_id))
    tickers = list(positions)
    prices = {}
    if tickers:
        for r in conn.execute("SELECT * FROM v_latest_prices WHERE ticker = ANY(%s)", (tickers,)):
            prices[r["ticker"]] = r
        names = conn.execute("SELECT ticker, name, sector_code FROM companies WHERE ticker = ANY(%s)",
                             (tickers,)).fetchall()
        for n in names:  # unpriced holdings still get a name/sector
            prices.setdefault(n["ticker"], {**n, "close": None, "trade_date": None})
    return summarise(positions, prices, load_other(conn, profile_id))
