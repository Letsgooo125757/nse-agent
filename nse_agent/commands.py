"""Phase 2 CLI commands: profile, plan, portfolio, transactions, watchlist, alerts."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import alerts as alerts_mod
from . import investor, planner, portfolio
from .config import NAIROBI_TZ, get_settings
from .db import connect
from .fees import dividend_tax, trade_fees
from .portfolio import PortfolioError, Txn, fmt_qty
from .sources.base import parse_date


def today() -> date:
    return datetime.now(NAIROBI_TZ).date()


def money(x) -> str:
    return "-" if x is None else f"{Decimal(x):,.2f}"


def dec(text: str) -> Decimal:
    try:
        return Decimal(str(text).replace(",", ""))
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")


def table(rows: list[list], headers: list[str], align: str | None = None) -> str:
    cells = [headers] + [[str(c) for c in r] for r in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]
    align = align or "l" + "r" * (len(headers) - 1)
    fmt = lambda r: "  ".join(c.ljust(w) if a == "l" else c.rjust(w)  # noqa: E731
                              for c, w, a in zip(r, widths, align))
    return "\n".join([fmt(cells[0]), "  ".join("-" * w for w in widths), *map(fmt, cells[1:])])


def _pid(conn, args) -> int:
    return investor.resolve_profile_id(conn, getattr(args, "profile", None))


# ---------------------------------------------------------------- profile


def cmd_profile_create(args) -> int:
    if args.from_file:
        prof, savings = investor.profile_from_dict(json.loads(Path(args.from_file).read_text()))
    else:
        prof, savings = investor.run_questionnaire()
    with connect() as conn:
        if args.update is not None:
            prof.id = args.update
            investor.load_profile(conn, prof.id)  # must exist
        pid = investor.save_profile(conn, prof)
        investor.set_emergency_fund(conn, pid, savings)
    print(f"\nProfile #{pid} saved: risk score {prof.risk_score}/100 ({prof.risk_tolerance}), "
          f"horizon ~{prof.horizon_years} years.")
    print("Next: `nse-agent plan`")
    return 0


def cmd_profile_show(args) -> int:
    with connect() as conn:
        p = investor.load_profile(conn, _pid(conn, args))
        other = portfolio.load_other(conn, p.id)
    print(f"Profile #{p.id}: {p.display_name}")
    print(f"  Risk score        {p.risk_score}/100 ({p.risk_tolerance})")
    print(f"  Horizon           ~{p.horizon_years} years")
    print(f"  Monthly income    {money(p.monthly_income)}")
    print(f"  Monthly expenses  {money(p.monthly_expenses)}")
    print(f"  Monthly to invest {money(p.monthly_investable)}")
    print(f"  High-interest debt {money(p.high_interest_debt)}")
    print(f"  Emergency fund    {money(other.get('emergency_cash', 0))} "
          f"(target {p.emergency_months_target} months of expenses)")
    print(f"  Limits            {p.max_stock_pct:.0f}% per stock, {p.max_sector_pct:.0f}% per sector")
    if p.goals:
        print(f"  Goal              {p.goals}")
    print("\nAnswers:")
    for q in investor.QUESTIONS:
        print(f"  {q.text}\n    -> {q.options[p.answers[q.key]][0]}")
    return 0


def cmd_profile_list(args) -> int:
    with connect() as conn:
        rows = conn.execute("SELECT id, display_name, risk_score, risk_tolerance, horizon_years "
                            "FROM investor_profiles ORDER BY id").fetchall()
    print(table([[r["id"], r["display_name"], r["risk_score"], r["risk_tolerance"], r["horizon_years"]]
                 for r in rows], ["id", "name", "score", "tolerance", "horizon"], "rllll"))
    return 0


# ---------------------------------------------------------------- plan


def cmd_plan(args) -> int:
    with connect() as conn:
        pid = _pid(conn, args)
        p = investor.load_profile(conn, pid)
        s = portfolio.snapshot(conn, pid)
        plan = planner.build_plan(p, s.class_values())
        conn.execute("DELETE FROM allocation_targets WHERE profile_id = %s", (pid,))
        for c, pct in plan.targets.items():
            conn.execute("INSERT INTO allocation_targets(profile_id, asset_class, target_pct) "
                         "VALUES (%s, %s, %s)", (pid, c, pct))

    print(f"Plan for {p.display_name}: risk {p.risk_score}/100 ({p.risk_tolerance}), "
          f"horizon ~{p.horizon_years}y, KES {p.monthly_investable:,.0f}/month\n")
    print("Priorities:")
    for i, step in enumerate(plan.steps, 1):
        amt = f"  [KES {step.monthly_amount:,.0f}/month]" if step.monthly_amount else ""
        print(f"  {i}. {step.title}{amt}\n     {step.detail}")
    print("\nTarget mix for long-term money:")
    rows = [[planner.LABELS[c], f"{plan.targets[c]}%",
             f"{plan.current_pct[c]}%" if plan.current_pct else "-"] for c in planner.INVEST_CLASSES]
    print(table(rows, ["asset class", "target", "now"]))
    print("\nWhy:")
    for r in plan.reasoning:
        print(f"  - {r}")
    if plan.monthly_split:
        print("\nThis month's money:")
        print(table([[planner.LABELS[k], money(v)] for k, v in plan.monthly_split.items()],
                    ["where", "KES"]))
    print("\nRules for the share portion:")
    for r in plan.equity_rules:
        print(f"  - {r}")
    for w in plan.warnings:
        print(f"\n  warning: {w}")
    print("\nThis plan follows fixed rules for your own research. It is not licensed "
          "investment advice.")
    return 0


# ---------------------------------------------------------------- transactions


def _record(args, side: str, quantity: Decimal, price: Decimal, fees: Decimal, tax: Decimal = Decimal(0)) -> int:
    t = Txn(ticker=args.ticker.upper(), side=side, trade_date=args.date or today(),
            quantity=quantity, price=price, fees=fees, tax=tax)
    with connect() as conn:
        known = conn.execute("SELECT 1 FROM companies WHERE ticker = %s", (t.ticker,)).fetchone()
        if not known:
            raise PortfolioError(f"Unknown ticker {t.ticker}. Check `nse-agent companies`")
        tid = portfolio.add_txn(conn, _pid(conn, args), t, getattr(args, "notes", None))
    return tid


def cmd_trade(args) -> int:
    consideration = args.quantity * args.price
    fb = trade_fees(consideration) if args.fees is None else None
    fees = fb.total if fb else args.fees
    tid = _record(args, args.side, args.quantity, args.price, fees)
    verb = "Bought" if args.side == "buy" else "Sold"
    print(f"{verb} {fmt_qty(args.quantity)} {args.ticker.upper()} @ {args.price} "
          f"= KES {consideration:,.2f} (+fees {fees}). Transaction #{tid}.")
    if fb:
        detail = ", ".join(f"{n} {v}" for n, v in fb.lines()[:-1] if v)
        print(f"  Estimated fees KES {fees} ({fb.total_pct}%): {detail}")
        print("  (use --fees with the exact figure from your contract note if it differs)")
    return 0


def cmd_dividend(args) -> int:
    with connect() as conn:
        pid = _pid(conn, args)
        shares = args.shares
        if shares is None:
            txns = portfolio.load_txns(conn, pid)
            shares = portfolio.shares_held_on(txns, args.ticker.upper(), args.date or today())
    if not shares:
        raise PortfolioError(f"You held no {args.ticker.upper()} shares on that date. Pass --shares")
    gross = shares * args.dps
    tax = args.tax if args.tax is not None else dividend_tax(gross)
    tid = _record(args, "dividend", shares, args.dps, Decimal(0), tax)
    print(f"Dividend: {fmt_qty(shares)} x KES {args.dps} = KES {gross:,.2f} gross, tax {tax}, "
          f"net KES {gross - tax:,.2f}. Transaction #{tid}.")
    return 0


def cmd_bonus(args) -> int:
    """e.g. `bonus SCOM 1 10` = 1 new share for every 10 held."""
    with connect() as conn:
        pid = _pid(conn, args)
        held = portfolio.shares_held_on(portfolio.load_txns(conn, pid), args.ticker.upper(),
                                        args.date or today())
    new = (held * args.new // args.old)
    if new <= 0:
        raise PortfolioError("Bonus works out to 0 new shares")
    tid = _record(args, "bonus", new, Decimal(0), Decimal(0))
    print(f"Bonus {args.new}:{args.old} on {fmt_qty(held)} shares -> {fmt_qty(new)} new shares. Transaction #{tid}.")
    return 0


def cmd_import_transactions(args) -> int:
    """CSV columns: date, ticker, side, quantity, price[, fees, tax, notes].
    Blank fees on buy/sell are estimated; blank tax on dividends = 5% WHT."""
    written = 0
    with connect() as conn, args.file.open(newline="", encoding="utf-8-sig") as fh:
        pid = _pid(conn, args)
        for lineno, row in enumerate(csv.DictReader(fh), start=2):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            side = row["side"].lower()
            qty, price = dec(row["quantity"]), dec(row["price"])
            fees = dec(row["fees"]) if row.get("fees") else (
                trade_fees(qty * price).total if side in ("buy", "sell") else Decimal(0))
            tax = dec(row["tax"]) if row.get("tax") else (
                dividend_tax(qty * price) if side == "dividend" else Decimal(0))
            t = Txn(row["ticker"].upper(), side, parse_date(row["date"]), qty, price, fees, tax)
            try:
                portfolio.add_txn(conn, pid, t, row.get("notes") or None)
            except PortfolioError as exc:
                raise PortfolioError(f"line {lineno}: {exc}") from exc
            written += 1
    print(f"Imported {written} transactions.")
    return 0


def cmd_transactions(args) -> int:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, trade_date, side, ticker, quantity, price, fees, tax FROM transactions "
            "WHERE profile_id = %s ORDER BY trade_date, id", (_pid(conn, args),)).fetchall()
    print(table([[r["id"], r["trade_date"], r["side"], r["ticker"], fmt_qty(r["quantity"]),
                  r["price"], r["fees"], r["tax"]] for r in rows],
                ["id", "date", "side", "ticker", "qty", "price", "fees", "tax"], "rllllrrr"))
    return 0


def cmd_delete_transaction(args) -> int:
    with connect() as conn:
        pid = _pid(conn, args)
        t = conn.execute("DELETE FROM transactions WHERE id = %s AND profile_id = %s RETURNING id",
                         (args.id, pid)).fetchone()
        portfolio.build_positions(portfolio.load_txns(conn, pid))  # still consistent?
    print(f"Deleted transaction #{args.id}." if t else f"No transaction #{args.id}.")
    return 0 if t else 1


# ---------------------------------------------------------------- other holdings


def cmd_holding_set(args) -> int:
    with connect() as conn:
        pid = _pid(conn, args)
        conn.execute(
            """INSERT INTO other_holdings(profile_id, asset_class, name, amount, expected_yield_pct, maturity_date)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (profile_id, asset_class, name) DO UPDATE SET amount = EXCLUDED.amount,
                   expected_yield_pct = COALESCE(EXCLUDED.expected_yield_pct, other_holdings.expected_yield_pct),
                   maturity_date = COALESCE(EXCLUDED.maturity_date, other_holdings.maturity_date),
                   as_of = CURRENT_DATE""",
            (pid, args.asset_class, args.name, args.amount, args.yield_pct, args.maturity))
    print(f"{args.name} ({args.asset_class}) set to KES {args.amount:,.2f}.")
    return 0


def cmd_holding_list(args) -> int:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM other_holdings WHERE profile_id = %s ORDER BY asset_class, name",
                            (_pid(conn, args),)).fetchall()
    print(table([[r["asset_class"], r["name"], money(r["amount"]),
                  f"{r['expected_yield_pct']}%" if r["expected_yield_pct"] is not None else "-",
                  r["maturity_date"] or "-", r["as_of"]] for r in rows],
                ["class", "name", "KES", "yield", "matures", "as of"], "llrrrr"))
    return 0


# ---------------------------------------------------------------- portfolio


def cmd_portfolio(args) -> int:
    with connect() as conn:
        s = portfolio.snapshot(conn, _pid(conn, args))
    if not s.holdings and not s.other:
        print("No holdings yet. Record a trade with `nse-agent buy TICKER QTY PRICE`.")
        return 0
    if s.holdings:
        print(table([[h.ticker, f"{h.shares:,.0f}", money(h.avg_cost), money(h.price),
                      money(h.market_value), money(h.unrealised_pnl),
                      f"{h.unrealised_pct}%" if h.unrealised_pct is not None else "-",
                      f"{h.weight_pct}%", money(h.dividends_net), h.price_date or "-"]
                     for h in s.holdings],
                    ["ticker", "shares", "avg cost", "price", "value", "gain", "gain%", "weight",
                     "divs (net)", "price date"]))
        print(f"\nShares: value KES {money(s.equity_value)}, cost {money(s.equity_cost)}, "
              f"unrealised {money(s.unrealised_pnl)}")
        print(f"Realised gains {money(s.realised_pnl)}, dividends received (net) {money(s.dividends_net)}, "
              f"total return {money(s.total_return)}")
        exit_fees = sum((h.exit_fees for h in s.holdings), Decimal(0))
        print(f"Selling everything today would cost about KES {money(exit_fees)} in fees.")
        print("\nBy sector: " + ", ".join(f"{k.title()} {v}%" for k, v in s.sector_pct.items()))
    if s.other:
        print("\nOther holdings: " + ", ".join(f"{planner.LABELS.get(k, k)} KES {money(v)}"
                                               for k, v in s.other.items()))
    total = sum(s.class_values().values(), Decimal(0))
    print(f"\nTotal tracked wealth: KES {money(total)}")
    for w in s.warnings:
        print(f"  warning: {w}")
    return 0


def cmd_companies(args) -> int:
    with connect() as conn:
        rows = conn.execute("SELECT ticker, name, sector_code, is_active FROM companies "
                            "WHERE %s::text IS NULL OR sector_code = upper(%s::text) ORDER BY ticker",
                            (args.sector, args.sector)).fetchall()
    print(table([[r["ticker"], r["name"], r["sector_code"] or "-", "" if r["is_active"] else "inactive"]
                 for r in rows], ["ticker", "name", "sector", ""], "llll"))
    return 0


def cmd_fees(args) -> int:
    fb = trade_fees(args.amount)
    print(table([[n, money(v)] for n, v in fb.lines()], ["item", "KES"]))
    print(f"\n{fb.total_pct}% of KES {money(args.amount)}, charged on the buy and again on the sell.")
    return 0


# ---------------------------------------------------------------- watchlist & alerts


def cmd_watch(args) -> int:
    with connect() as conn:
        pid = _pid(conn, args)
        for t in args.tickers:
            if args.remove:
                conn.execute("DELETE FROM watchlist WHERE profile_id = %s AND ticker = %s", (pid, t.upper()))
            else:
                conn.execute("INSERT INTO watchlist(profile_id, ticker) VALUES (%s, %s) "
                             "ON CONFLICT DO NOTHING", (pid, t.upper()))
        rows = conn.execute("SELECT ticker FROM watchlist WHERE profile_id = %s ORDER BY ticker", (pid,))
        print("Watchlist:", ", ".join(r["ticker"] for r in rows) or "(empty)")
    return 0


def cmd_alert_rule(args) -> int:
    if args.action == "add" and not (args.ticker and args.condition and args.threshold):
        raise PortfolioError("Usage: alert-rule add TICKER above|below|move_pct THRESHOLD")
    with connect() as conn:
        pid = _pid(conn, args)
        if args.action == "add":
            conn.execute(
                """INSERT INTO price_alert_rules(profile_id, ticker, condition, threshold)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (profile_id, ticker, condition, threshold)
                   DO UPDATE SET active = TRUE""",
                (pid, args.ticker.upper(), args.condition, args.threshold))
        rows = conn.execute("SELECT * FROM price_alert_rules WHERE profile_id = %s AND active ORDER BY id",
                            (pid,)).fetchall()
    print(table([[r["id"], r["ticker"], r["condition"], r["threshold"]] for r in rows],
                ["id", "ticker", "condition", "threshold"], "rllr"))
    return 0


def cmd_alerts_run(args) -> int:
    with connect() as conn:
        created = alerts_mod.run_alerts(conn, _pid(conn, args), today=args.date or today(),
                                        stale_after_days=get_settings().stale_after_days)
    if not created:
        print("No new alerts.")
    for a in created:
        print(f"[{a['severity'].upper():7}] {a['message']}")
    return 0


def cmd_alerts_list(args) -> int:
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM alerts WHERE profile_id = %s
                {'' if args.all else 'AND acknowledged_at IS NULL'}
                ORDER BY created_at DESC LIMIT %s""", (_pid(conn, args), args.limit)).fetchall()
    if not rows:
        print("No open alerts.")
    for a in rows:
        done = " (done)" if a["acknowledged_at"] else ""
        print(f"#{a['id']:<4} {a['created_at']:%Y-%m-%d} [{a['severity']}] {a['message']}{done}")
    return 0


def cmd_alerts_ack(args) -> int:
    with connect() as conn:
        pid = _pid(conn, args)
        if args.ids == ["all"]:
            n = conn.execute("UPDATE alerts SET acknowledged_at = now() WHERE profile_id = %s "
                             "AND acknowledged_at IS NULL", (pid,)).rowcount
        else:
            n = conn.execute("UPDATE alerts SET acknowledged_at = now() WHERE profile_id = %s "
                             "AND id = ANY(%s)", (pid, [int(i) for i in args.ids])).rowcount
    print(f"Marked {n} alert(s) as done.")
    return 0


# ---------------------------------------------------------------- registration


def register(sub) -> None:
    def with_profile(p):
        p.add_argument("--profile", type=int, help="profile id (default: your first profile)")
        return p

    def date_arg(p, help_="trade date (default: today)"):
        p.add_argument("--date", type=parse_date, help=help_)

    prof = sub.add_parser("profile", help="create or view your investor profile")
    psub = prof.add_subparsers(dest="profile_cmd", required=True)
    p = psub.add_parser("create", help="answer the questionnaire (re-run with --update ID to change)")
    p.add_argument("--from-file", help="JSON answers instead of prompts (see templates/profile.json)")
    p.add_argument("--update", type=int, metavar="ID", help="overwrite an existing profile")
    p.set_defaults(func=cmd_profile_create)
    with_profile(psub.add_parser("show")).set_defaults(func=cmd_profile_show)
    psub.add_parser("list").set_defaults(func=cmd_profile_list)

    with_profile(sub.add_parser("plan", help="what to do with this month's money")).set_defaults(func=cmd_plan)
    with_profile(sub.add_parser("portfolio", help="holdings, gains, dividends, exposure")).set_defaults(func=cmd_portfolio)

    for side in ("buy", "sell"):
        p = with_profile(sub.add_parser(side, help=f"record a {side}"))
        p.add_argument("ticker")
        p.add_argument("quantity", type=dec)
        p.add_argument("price", type=dec)
        p.add_argument("--fees", type=dec, help="actual total fees (default: estimated)")
        p.add_argument("--notes")
        date_arg(p)
        p.set_defaults(func=cmd_trade, side=side)

    p = with_profile(sub.add_parser("dividend", help="record a dividend received"))
    p.add_argument("ticker")
    p.add_argument("dps", type=dec, help="dividend per share (KES)")
    p.add_argument("--shares", type=dec, help="shares entitled (default: shares held on --date)")
    p.add_argument("--tax", type=dec, help="withholding tax deducted (default: 5%%)")
    date_arg(p, "payment or book-closure date (default: today)")
    p.set_defaults(func=cmd_dividend)

    p = with_profile(sub.add_parser("bonus", help="record a bonus issue, e.g. `bonus KCB 1 10`"))
    p.add_argument("ticker")
    p.add_argument("new", type=int)
    p.add_argument("old", type=int)
    date_arg(p)
    p.set_defaults(func=cmd_bonus)

    p = with_profile(sub.add_parser("import-transactions", help="load trades from CSV"))
    p.add_argument("file", type=Path)
    p.set_defaults(func=cmd_import_transactions)
    with_profile(sub.add_parser("transactions", help="list recorded transactions")).set_defaults(func=cmd_transactions)
    p = with_profile(sub.add_parser("delete-transaction"))
    p.add_argument("id", type=int)
    p.set_defaults(func=cmd_delete_transaction)

    hold = sub.add_parser("holding", help="money outside NSE shares (MMF, T-bills, cash, SACCO)")
    hsub = hold.add_subparsers(dest="holding_cmd", required=True)
    p = with_profile(hsub.add_parser("set"))
    p.add_argument("asset_class", choices=["emergency_cash", "money_market", "government_securities",
                                           "sacco", "other"])
    p.add_argument("name")
    p.add_argument("amount", type=dec)
    p.add_argument("--yield-pct", type=dec)
    p.add_argument("--maturity", type=parse_date)
    p.set_defaults(func=cmd_holding_set)
    with_profile(hsub.add_parser("list")).set_defaults(func=cmd_holding_list)

    p = sub.add_parser("companies", help="list NSE securities and tickers")
    p.add_argument("--sector", help="e.g. BANKING")
    p.set_defaults(func=cmd_companies)

    p = sub.add_parser("fees", help="trading cost breakdown for an amount")
    p.add_argument("amount", type=dec)
    p.set_defaults(func=cmd_fees)

    p = with_profile(sub.add_parser("watch", help="add (or --remove) tickers on your watchlist"))
    p.add_argument("tickers", nargs="*")
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_watch)

    p = with_profile(sub.add_parser("alert-rule", help="price alerts, e.g. `alert-rule add SCOM below 25`"))
    p.add_argument("action", choices=["add", "list"])
    p.add_argument("ticker", nargs="?")
    p.add_argument("condition", nargs="?", choices=["above", "below", "move_pct"])
    p.add_argument("threshold", nargs="?", type=dec)
    p.set_defaults(func=cmd_alert_rule)

    al = sub.add_parser("alerts", help="run, list or acknowledge alerts")
    asub = al.add_subparsers(dest="alerts_cmd", required=True)
    p = with_profile(asub.add_parser("run"))
    date_arg(p, "evaluate as of this date (default: today)")
    p.set_defaults(func=cmd_alerts_run)
    p = with_profile(asub.add_parser("list"))
    p.add_argument("--all", action="store_true", help="include acknowledged")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_alerts_list)
    p = with_profile(asub.add_parser("ack"))
    p.add_argument("ids", nargs="+", help="alert ids, or 'all'")
    p.set_defaults(func=cmd_alerts_ack)
