"""NSE equity trading costs.

Rates as published by nsecalc.co.ke (checked September 2026). They change
from time to time, so every rate is overridable from the environment:

  brokerage      1.00-1.50% depending on broker   (BROKERAGE_PCT, BROKERAGE_MIN)
  NSE levy       0.12%
  CMA levy       0.08%
  CDSC levy      0.08%
  ICF levy       0.01%
  VAT            16% of brokerage                  (VAT_ON_BROKERAGE_PCT)
  stamp duty     KES 2 per KES 10,000 (or part) of consideration

Costs apply to both buys and sells. Dividend withholding tax for resident
individuals is 5% (PwC Worldwide Tax Summaries, reviewed July 2026).
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def _env_pct(name: str, default: str) -> Decimal:
    return Decimal(os.getenv(name, default)) / 100


@dataclass(frozen=True)
class FeeSchedule:
    brokerage: Decimal = Decimal("0.015")
    brokerage_min: Decimal = Decimal("0")
    nse_levy: Decimal = Decimal("0.0012")
    cma_levy: Decimal = Decimal("0.0008")
    cdsc_levy: Decimal = Decimal("0.0008")
    icf_levy: Decimal = Decimal("0.0001")
    vat_on_brokerage: Decimal = Decimal("0.16")
    stamp_duty_per_bracket: Decimal = Decimal("2")
    stamp_duty_bracket: Decimal = Decimal("10000")
    dividend_wht: Decimal = Decimal("0.05")

    @classmethod
    def from_env(cls) -> "FeeSchedule":
        return cls(
            brokerage=_env_pct("BROKERAGE_PCT", "1.5"),
            brokerage_min=Decimal(os.getenv("BROKERAGE_MIN", "0")),
            vat_on_brokerage=_env_pct("VAT_ON_BROKERAGE_PCT", "16"),
            dividend_wht=_env_pct("DIVIDEND_WHT_PCT", "5"),
        )


@dataclass(frozen=True)
class FeeBreakdown:
    consideration: Decimal
    brokerage: Decimal
    vat: Decimal
    nse_levy: Decimal
    cma_levy: Decimal
    cdsc_levy: Decimal
    icf_levy: Decimal
    stamp_duty: Decimal

    @property
    def total(self) -> Decimal:
        return (self.brokerage + self.vat + self.nse_levy + self.cma_levy + self.cdsc_levy
                + self.icf_levy + self.stamp_duty)

    @property
    def total_pct(self) -> Decimal:
        if not self.consideration:
            return Decimal(0)
        return (100 * self.total / self.consideration).quantize(CENT)

    def lines(self) -> list[tuple[str, Decimal]]:
        return [("Brokerage", self.brokerage), ("VAT on brokerage", self.vat),
                ("NSE levy", self.nse_levy), ("CMA levy", self.cma_levy),
                ("CDSC levy", self.cdsc_levy), ("ICF levy", self.icf_levy),
                ("Stamp duty", self.stamp_duty), ("Total", self.total)]


def _r(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def trade_fees(consideration: Decimal, schedule: FeeSchedule | None = None) -> FeeBreakdown:
    """Costs for one buy or sell of `consideration` KES (quantity x price)."""
    s = schedule or FeeSchedule.from_env()
    c = Decimal(consideration)
    if c < 0:
        raise ValueError("consideration must be >= 0")
    brokerage = _r(max(c * s.brokerage, s.brokerage_min)) if c else Decimal(0)
    brackets = math.ceil(c / s.stamp_duty_bracket) if c else 0
    return FeeBreakdown(
        consideration=c,
        brokerage=brokerage,
        vat=_r(brokerage * s.vat_on_brokerage),
        nse_levy=_r(c * s.nse_levy),
        cma_levy=_r(c * s.cma_levy),
        cdsc_levy=_r(c * s.cdsc_levy),
        icf_levy=_r(c * s.icf_levy),
        stamp_duty=s.stamp_duty_per_bracket * brackets,
    )


def dividend_tax(gross: Decimal, schedule: FeeSchedule | None = None) -> Decimal:
    s = schedule or FeeSchedule.from_env()
    return _r(Decimal(gross) * s.dividend_wht)
