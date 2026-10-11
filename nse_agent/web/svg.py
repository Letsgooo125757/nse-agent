"""Small server-rendered SVG graphics (sparklines, range bars, allocation bars).

Server-side SVG means no JavaScript and no inline styles, which keeps the CSP
strict. Every graphic carries an aria-label with the numbers it shows.
"""
from __future__ import annotations

from decimal import Decimal
from html import escape
from typing import Sequence

from markupsafe import Markup

Num = float | int | Decimal


def _f(x: Num) -> float:
    return float(x)


def sparkline(values: Sequence[Num], *, width: int = 96, height: int = 24, label: str = "") -> Markup:
    """A single-series trend line with an end dot. Class names carry colour."""
    vals = [_f(v) for v in values if v is not None]
    if len(vals) < 2:
        return Markup('<span class="spark-empty">not enough history</span>')
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pad = 3
    step = (width - 2 * pad) / (len(vals) - 1)
    pts = [(pad + i * step, pad + (height - 2 * pad) * (1 - (v - lo) / span)) for i, v in enumerate(vals)]
    d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    trend = "up" if vals[-1] > vals[0] else "down" if vals[-1] < vals[0] else "flat"
    ex, ey = pts[-1]
    aria = escape(label or f"{len(vals)} closes from {vals[0]:,.2f} to {vals[-1]:,.2f}")
    return Markup(
        f'<svg class="spark spark-{trend}" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'role="img" aria-label="{aria}"><polyline points="{d}" fill="none" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round"/><circle cx="{ex:.1f}" cy="{ey:.1f}" r="3"/></svg>')


def range_bar(low: Num | None, high: Num | None, value: Num | None, *, width: int = 120) -> Markup:
    """Where today's price sits within its 52-week low–high range."""
    if low is None or high is None or value is None or _f(high) <= _f(low):
        return Markup('<span class="muted">–</span>')
    lo, hi, v = _f(low), _f(high), _f(value)
    pos = 4 + (width - 8) * min(max((v - lo) / (hi - lo), 0), 1)
    aria = escape(f"52-week range {lo:,.2f} to {hi:,.2f}; now {v:,.2f}")
    return Markup(
        f'<svg class="range" viewBox="0 0 {width} 14" width="{width}" height="14" role="img" aria-label="{aria}">'
        f'<rect class="range-track" x="4" y="6" width="{width - 8}" height="2" rx="1"/>'
        f'<circle class="range-dot" cx="{pos:.1f}" cy="7" r="4"/></svg>')


def alloc_bar(key: str, label: str, current: Num | None, target: Num | None, *, width: int = 400) -> Markup:
    """One horizontal bar: current % of long-term money, with a tick at the target %."""
    cur, tgt = _f(current or 0), _f(target or 0)
    h, y = 14, 5
    w = width * min(cur, 100) / 100
    tx = min(max(width * tgt / 100 - 1, 0), width - 2)
    aria = escape(f"{label}: {cur:.1f}% now, target {tgt:.0f}%")
    bar = (f'<rect class="alloc-bar series-{escape(key)}" x="0" y="{y}" width="{w:.1f}" height="{h}" rx="4"/>'
           if w > 0 else "")
    return Markup(
        f'<svg class="alloc" viewBox="0 0 {width} 24" preserveAspectRatio="none" role="img" aria-label="{aria}">'
        f'<rect class="alloc-track" x="0" y="{y}" width="{width}" height="{h}" rx="4"/>{bar}'
        f'<rect class="alloc-target" x="{tx:.1f}" y="1" width="2" height="22"/></svg>')
