"""The web app: `nse-agent web` locally, or `uvicorn nse_agent.web.app:app` when hosted.

Pages reuse the same functions as the CLI and the assistant's tools, so every
number on screen matches what `nse-agent portfolio` / `plan` / `ask` say.
"""
from __future__ import annotations

import json
import os
import secrets
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

import psycopg
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from starlette.middleware.sessions import SessionMiddleware

from .. import alerts as alerts_mod
from .. import portfolio as portfolio_mod
from ..agent import tools as T
from ..agent.chat import Agent, MissingApiKey, describe_api_error, make_client
from ..config import NAIROBI_TZ, get_settings
from ..db import resolve_url
from ..fees import trade_fees
from ..planner import INVEST_CLASSES, LABELS
from ..tagging import TOPIC_KEYWORDS
from . import svg
from .security import LoginThrottle, SecurityHeaders, csrf_ok, csrf_token, verify_password

HERE = Path(__file__).parent
SESSION_MAX_AGE = 7 * 24 * 3600


# ---------------------------------------------------------------- settings


@dataclass(frozen=True)
class WebSettings:
    password_hash: str
    session_secret: str
    cookie_secure: bool = True
    chat_daily_limit: int = 40
    database_url: str | None = None

    @classmethod
    def from_env(cls) -> "WebSettings":
        missing = [k for k in ("APP_PASSWORD_HASH", "SESSION_SECRET") if not os.getenv(k)]
        if missing:
            raise RuntimeError(
                f"Missing {', '.join(missing)}. Run `nse-agent web-setup` to generate both, "
                "then put them in .env (locally) or the host's environment settings.")
        if len(os.environ["SESSION_SECRET"]) < 32:
            raise RuntimeError("SESSION_SECRET must be at least 32 characters (use `nse-agent web-setup`).")
        return cls(
            password_hash=os.environ["APP_PASSWORD_HASH"],
            session_secret=os.environ["SESSION_SECRET"],
            cookie_secure=os.getenv("COOKIE_SECURE", "true").lower() != "false",
            chat_daily_limit=int(os.getenv("CHAT_DAILY_LIMIT", "40")),
        )


# ---------------------------------------------------------------- formatting


def fmt_money(x: Any, dp: int = 0) -> str:
    if x is None or x == "":
        return "–"
    return f"{Decimal(str(x)):,.{dp}f}"


def fmt_price(x: Any) -> str:
    return fmt_money(x, 2)


def fmt_pct(x: Any, dp: int = 1, sign: bool = False) -> str:
    if x is None:
        return "–"
    v = float(x)
    s = f"{v:+.{dp}f}" if sign else f"{v:.{dp}f}"
    return s.replace("-", "−") + "%"


def fmt_signed(x: Any, dp: int = 0) -> str:
    if x is None:
        return "–"
    v = Decimal(str(x))
    return ("+" if v > 0 else "−" if v < 0 else "") + f"{abs(v):,.{dp}f}"


def direction(x: Any) -> str:
    if x is None:
        return "flat"
    v = float(x)
    return "up" if v > 0 else "down" if v < 0 else "flat"


def fmt_date(d: Any) -> str:
    if not d:
        return "–"
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])
    return f"{d.day} {d:%b %Y}"


def fmt_qty(x: Any) -> str:
    return portfolio_mod.fmt_qty(Decimal(str(x))) if x is not None else "–"


# ---------------------------------------------------------------- app


class NeedsLogin(Exception):
    pass


def create_app(settings: WebSettings | None = None, *,
               llm_client_factory: Callable[[], Any] | None = None,
               today_fn: Callable[[], date] | None = None) -> FastAPI:
    settings = settings or WebSettings.from_env()
    llm_client_factory = llm_client_factory or make_client
    today_fn = today_fn or (lambda: datetime.now(NAIROBI_TZ).date())
    pool = ConnectionPool(resolve_url(settings.database_url), min_size=1, max_size=5, open=False,
                          kwargs={"row_factory": dict_row}, check=ConnectionPool.check_connection)
    throttle = LoginThrottle()
    agents: "OrderedDict[str, Agent]" = OrderedDict()

    @asynccontextmanager
    async def lifespan(_app):
        pool.open(wait=True, timeout=30)
        yield
        pool.close()

    app = FastAPI(title="NSE desk", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.pool = pool
    app.state.throttle = throttle
    app.add_middleware(SecurityHeaders, hsts=settings.cookie_secure)
    app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, session_cookie="nse_session",
                       max_age=SESSION_MAX_AGE, same_site="lax", https_only=settings.cookie_secure)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    templates = Jinja2Templates(directory=HERE / "templates")
    env = templates.env
    env.filters.update(money=fmt_money, price=fmt_price, pct=fmt_pct, signed=fmt_signed,
                       dir=direction, d=fmt_date, qty=fmt_qty)
    env.globals.update(sparkline=svg.sparkline, range_bar=svg.range_bar, alloc_bar=svg.alloc_bar,
                       csrf=csrf_token, LABELS=LABELS)

    # ------------------------------------------------------------ plumbing

    def conn_dep() -> Iterator[psycopg.Connection]:
        with pool.connection() as conn:
            yield conn

    def user_dep(request: Request) -> str:
        if request.session.get("user") != "owner":
            raise NeedsLogin()
        return "owner"

    @app.exception_handler(NeedsLogin)
    async def _needs_login(request: Request, _exc):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        url = f"/login?next={quote(target)}"
        if request.headers.get("HX-Request"):
            return Response(status_code=200, headers={"HX-Redirect": url})
        return RedirectResponse(url, status_code=303)

    async def csrf_dep(request: Request) -> None:
        submitted = request.headers.get("X-CSRF-Token")
        if submitted is None:
            form = await request.form()
            submitted = form.get("csrf_token")
        if not csrf_ok(request, submitted):
            raise HTTPException(403, "Your session expired or the form was stale. Reload the page and try again.")

    def ctx_for(conn: psycopg.Connection) -> T.ToolContext:
        pid = conn.execute("SELECT min(id) AS id FROM investor_profiles").fetchone()["id"]
        return T.ToolContext(conn=conn, profile_id=pid, today=today_fn(),
                             stale_after_days=get_settings().stale_after_days)

    def flash(request: Request, message: str, kind: str = "ok") -> None:
        request.session.setdefault("flash", []).append({"kind": kind, "text": message})

    def render(request: Request, name: str, ctx: dict, status: int = 200) -> HTMLResponse:
        messages = request.session.pop("flash", [])
        return templates.TemplateResponse(request, name, {"flashes": messages, "path": request.url.path,
                                                          "today": today_fn(), **ctx}, status_code=status)

    def closes(conn, tickers: list[str], days: int = 45) -> dict[str, list]:
        if not tickers:
            return {}
        rows = conn.execute(
            """SELECT ticker, close FROM daily_prices WHERE ticker = ANY(%s)
               AND trade_date >= (SELECT max(trade_date) FROM daily_prices) - %s::int
               ORDER BY ticker, trade_date""", (tickers, days)).fetchall()
        out: dict[str, list] = {}
        for r in rows:
            out.setdefault(r["ticker"], []).append(r["close"])
        return out

    def board(conn, ctx: T.ToolContext) -> list[dict]:
        tickers: list[str] = []
        if ctx.profile_id is not None:
            held = [h.ticker for h in portfolio_mod.snapshot(conn, ctx.profile_id).holdings]
            watch = [r["ticker"] for r in conn.execute(
                "SELECT ticker FROM watchlist WHERE profile_id = %s ORDER BY ticker", (ctx.profile_id,))]
            tickers = held + [t for t in watch if t not in held]
        if not tickers:  # nothing tracked yet: show the most valuable listed companies with prices
            tickers = [r["ticker"] for r in conn.execute(
                """SELECT ticker FROM v_latest_prices ORDER BY market_cap DESC NULLS LAST,
                   avg_volume_52w DESC NULLS LAST LIMIT 8""")]
        rows = {r["ticker"]: r for r in conn.execute(
            "SELECT * FROM v_latest_prices WHERE ticker = ANY(%s)", (tickers,))}
        series = closes(conn, tickers)
        return [{**rows[t], "closes": series.get(t, [])} for t in tickers if t in rows][:12]

    # ------------------------------------------------------------ auth

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/"):
        return render(request, "login.html", {"next": _safe_next(next), "error": None})

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, password: str = Form(...), next: str = Form("/"),
              csrf_token_: str = Form("", alias="csrf_token")):
        ip = request.client.host if request.client else "unknown"
        if not csrf_ok(request, csrf_token_):
            return render(request, "login.html", {"next": _safe_next(next),
                                                  "error": "The form expired. Try again."}, 403)
        wait = throttle.blocked(ip)
        if wait:
            return render(request, "login.html", {"next": _safe_next(next), "error":
                          f"Too many attempts. Try again in {max(1, wait // 60)} minute(s)."}, 429)
        if not verify_password(password, settings.password_hash):
            throttle.fail(ip)
            return render(request, "login.html", {"next": _safe_next(next),
                                                  "error": "That password isn't right."}, 401)
        throttle.reset(ip)
        request.session.clear()                       # new session on login (no fixation)
        request.session["user"] = "owner"
        request.session["csrf"] = secrets.token_urlsafe(32)
        return RedirectResponse(_safe_next(next), status_code=303)

    @app.post("/logout", dependencies=[Depends(csrf_dep)])
    def logout(request: Request):
        cid = request.session.get("chat_id")
        if cid:
            agents.pop(cid, None)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    def healthz():
        try:
            with pool.connection(timeout=10) as conn:
                conn.execute("SELECT 1")
            return {"ok": True}
        except Exception:
            return JSONResponse({"ok": False}, status_code=503)

    # ------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, _u=Depends(user_dep), conn=Depends(conn_dep)):
        ctx = ctx_for(conn)
        data: dict[str, Any] = {"board": board(conn, ctx), "status": T.data_status(ctx)["summary"]}
        if ctx.profile_id is not None:
            data["pf"] = T.get_my_portfolio(ctx)
            data["plan"] = T.get_my_plan(ctx)
            data["alerts"] = T.get_alerts(ctx, limit=5)["alerts"]
        data["dividends"] = T.get_upcoming_dividends(ctx, days=60)["dividends"]
        data["notes"] = conn.execute(
            """SELECT x.ticker, x.sentiment, x.why, a.title, a.url, a.source_code, a.published_at
               FROM article_analysis x JOIN news_articles a ON a.id = x.article_id
               WHERE COALESCE(a.published_at, a.fetched_at) >= %s
               ORDER BY (x.sentiment = 'neutral'), COALESCE(a.published_at, a.fetched_at) DESC LIMIT 5""",
            (datetime.combine(ctx.today - timedelta(days=7), datetime.min.time()),)).fetchall()
        data["tickers"] = [r["ticker"] for r in conn.execute(
            "SELECT ticker FROM companies WHERE is_active ORDER BY ticker")]
        return render(request, "overview.html", data)

    @app.get("/market", response_class=HTMLResponse)
    def market(request: Request, sector: str | None = None, sort: str = "value",
               _u=Depends(user_dep), conn=Depends(conn_dep)):
        order = {"value": "lp.market_cap DESC NULLS LAST, lp.avg_volume_52w DESC NULLS LAST",
                 "change": "lp.change_pct DESC NULLS LAST", "yield": "v.dividend_yield_pct DESC NULLS LAST",
                 "pe": "v.pe_ratio ASC NULLS LAST", "ticker": "lp.ticker"}.get(sort, "lp.ticker")
        rows = conn.execute(
            f"""SELECT lp.*, v.pe_ratio, v.dividend_yield_pct, s.name AS sector_name
                FROM v_latest_prices lp JOIN v_valuation v USING (ticker)
                JOIN companies c USING (ticker) LEFT JOIN sectors s ON s.sector_code = c.sector_code
                WHERE c.is_active AND (%s::text IS NULL OR c.sector_code = %s::text)
                ORDER BY {order}""", (sector, sector)).fetchall()
        series = closes(conn, [r["ticker"] for r in rows])
        sectors = conn.execute("SELECT sector_code, name FROM sectors ORDER BY name").fetchall()
        status = T.data_status(ctx_for(conn))["summary"]
        return render(request, "market.html", {"rows": rows, "series": series, "sectors": sectors,
                                               "sector": sector, "sort": sort, "status": status})

    @app.get("/company/{ticker}", response_class=HTMLResponse)
    def company(request: Request, ticker: str, _u=Depends(user_dep), conn=Depends(conn_dep)):
        t = ticker.upper()
        co = conn.execute("""SELECT c.*, s.name AS sector_name FROM companies c
                             LEFT JOIN sectors s USING (sector_code) WHERE c.ticker = %s""", (t,)).fetchone()
        if not co:
            raise HTTPException(404, f"No company with ticker {t}")
        ctx = ctx_for(conn)
        quote_ = conn.execute("SELECT * FROM v_latest_prices WHERE ticker = %s", (t,)).fetchone()
        val = conn.execute("SELECT * FROM v_valuation WHERE ticker = %s", (t,)).fetchone()
        hist = conn.execute("""SELECT trade_date, close FROM daily_prices WHERE ticker = %s
                               ORDER BY trade_date DESC LIMIT 500""", (t,)).fetchall()[::-1]
        fins = conn.execute("""SELECT * FROM financial_statements WHERE ticker = %s
                               ORDER BY period_end DESC LIMIT 6""", (t,)).fetchall()
        divs = conn.execute("""SELECT * FROM corporate_actions WHERE ticker = %s
                               ORDER BY COALESCE(book_closure_date, payment_date, announced_date) DESC NULLS LAST
                               LIMIT 8""", (t,)).fetchall()
        news = T.search_news(ctx, ticker=t, days=90, limit=8)["articles"]
        position = None
        watching = False
        if ctx.profile_id is not None:
            position = next((h for h in T.get_my_portfolio(ctx)["holdings"] if h["ticker"] == t), None)
            watching = bool(conn.execute("SELECT 1 FROM watchlist WHERE profile_id = %s AND ticker = %s",
                                         (ctx.profile_id, t)).fetchone())
        chart = json.dumps({"label": f"{t} close (KES)",
                            "points": [[r["trade_date"].isoformat(), float(r["close"])] for r in hist]}
                           ).replace("<", "\\u003c")  # safe inside <script>
        return render(request, "company.html", {"co": co, "q": quote_, "val": val, "chart": chart,
                                                "hist_n": len(hist), "fins": fins, "divs": divs,
                                                "news": news, "position": position, "watching": watching,
                                                "has_profile": ctx.profile_id is not None})

    @app.get("/plan", response_class=HTMLResponse)
    def plan(request: Request, _u=Depends(user_dep), conn=Depends(conn_dep)):
        ctx = ctx_for(conn)
        if ctx.profile_id is None:
            return render(request, "plan.html", {"plan": None})
        p = T.get_my_plan(ctx)
        alloc = [{"key": c, "label": LABELS[c], "current": p["current_mix_pct"].get(c),
                  "target": p["target_mix_pct"].get(c)} for c in INVEST_CLASSES]
        return render(request, "plan.html", {"plan": p, "alloc": alloc})

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts_page(request: Request, _u=Depends(user_dep), conn=Depends(conn_dep)):
        ctx = ctx_for(conn)
        if ctx.profile_id is None:
            return render(request, "alerts.html", {"open": [], "done": [], "has_profile": False})
        open_ = T.get_alerts(ctx, limit=50)["alerts"]
        done = conn.execute("""SELECT * FROM alerts WHERE profile_id = %s AND acknowledged_at IS NOT NULL
                               ORDER BY acknowledged_at DESC LIMIT 20""", (ctx.profile_id,)).fetchall()
        return render(request, "alerts.html", {"open": open_, "done": done, "has_profile": True})

    @app.post("/alerts/{alert_id}/done", dependencies=[Depends(csrf_dep)])
    def alert_done(request: Request, alert_id: int, _u=Depends(user_dep), conn=Depends(conn_dep)):
        ctx = ctx_for(conn)
        conn.execute("UPDATE alerts SET acknowledged_at = now() WHERE id = %s AND profile_id = %s",
                     (alert_id, ctx.profile_id))
        if request.headers.get("HX-Request"):
            return HTMLResponse("")                     # row disappears
        return RedirectResponse("/alerts", status_code=303)

    @app.post("/alerts/check", dependencies=[Depends(csrf_dep)])
    def alerts_check(request: Request, _u=Depends(user_dep), conn=Depends(conn_dep)):
        ctx = ctx_for(conn)
        if ctx.profile_id is None:
            flash(request, "Create your profile first: run `nse-agent profile create`.", "error")
        else:
            new = alerts_mod.run_alerts(conn, ctx.profile_id, today=ctx.today,
                                        stale_after_days=ctx.stale_after_days)
            flash(request, f"Checked. {len(new)} new alert{'s' if len(new) != 1 else ''}.")
        return RedirectResponse("/alerts", status_code=303)

    @app.get("/news", response_class=HTMLResponse)
    def news_page(request: Request, ticker: str | None = None, topic: str | None = None,
                  _u=Depends(user_dep), conn=Depends(conn_dep)):
        ctx = ctx_for(conn)
        ticker = ticker.upper() if ticker else None
        topic = topic if topic in TOPIC_KEYWORDS else None
        try:
            res = T.search_news(ctx, ticker=ticker, topic=topic, days=14, limit=25)
            error = None
        except T.ToolError as exc:
            res, error = {"articles": []}, str(exc)
        mine: list[str] = []
        if ctx.profile_id is not None:
            mine = [h["ticker"] for h in T.get_my_portfolio(ctx)["holdings"]] + [r["ticker"] for r in conn.execute(
                "SELECT ticker FROM watchlist WHERE profile_id = %s ORDER BY ticker", (ctx.profile_id,))]
        month = conn.execute(
            """SELECT COALESCE(sum(news_articles), 0) AS n, COALESCE(sum(news_cost_usd), 0) AS usd
               FROM app_usage WHERE date_trunc('month', day) = date_trunc('month', %s::date)""",
            (ctx.today,)).fetchone()
        return render(request, "news.html", {"articles": res["articles"], "ticker": ticker, "topic": topic,
                                             "month": month,
                                             "topics": sorted(TOPIC_KEYWORDS), "mine": list(dict.fromkeys(mine)),
                                             "error": error})

    # ------------------------------------------------------------ changes

    @app.post("/trades", dependencies=[Depends(csrf_dep)])
    def record_trade(request: Request, side: str = Form(...), ticker: str = Form(...),
                     quantity: str = Form(...), price: str = Form(...), fees: str = Form(""),
                     trade_date: str = Form(""), _u=Depends(user_dep), conn=Depends(conn_dep)):
        ctx = ctx_for(conn)
        back = request.headers.get("referer") or "/"
        back = back if _same_site(request, back) else "/"
        try:
            if ctx.profile_id is None:
                raise ValueError("Create your profile first: run `nse-agent profile create`.")
            if side not in ("buy", "sell"):
                raise ValueError("Choose buy or sell.")
            q, p = _dec(quantity, "Quantity"), _dec(price, "Price")
            if q <= 0 or p <= 0:
                raise ValueError("Quantity and price must be more than zero.")
            f = _dec(fees, "Fees") if fees.strip() else trade_fees(q * p).total
            d = date.fromisoformat(trade_date) if trade_date else ctx.today
            t = ticker.strip().upper()
            if not conn.execute("SELECT 1 FROM companies WHERE ticker = %s", (t,)).fetchone():
                raise ValueError(f"Unknown ticker {t}.")
            portfolio_mod.add_txn(conn, ctx.profile_id, portfolio_mod.Txn(t, side, d, q, p, f))
            flash(request, f"{'Bought' if side == 'buy' else 'Sold'} {fmt_qty(q)} {t} at KES {p:,.2f} "
                           f"(fees KES {f:,.2f}).")
        except (ValueError, portfolio_mod.PortfolioError) as exc:
            conn.rollback()
            flash(request, str(exc), "error")
        return RedirectResponse(back, status_code=303)

    @app.post("/watchlist", dependencies=[Depends(csrf_dep)])
    def watchlist(request: Request, ticker: str = Form(...), action: str = Form("add"),
                  _u=Depends(user_dep), conn=Depends(conn_dep)):
        ctx = ctx_for(conn)
        t = ticker.strip().upper()
        if ctx.profile_id is not None and conn.execute("SELECT 1 FROM companies WHERE ticker = %s", (t,)).fetchone():
            if action == "remove":
                conn.execute("DELETE FROM watchlist WHERE profile_id = %s AND ticker = %s", (ctx.profile_id, t))
                flash(request, f"Removed {t} from your watchlist.")
            else:
                conn.execute("INSERT INTO watchlist(profile_id, ticker) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                             (ctx.profile_id, t))
                flash(request, f"Added {t} to your watchlist.")
        return RedirectResponse(f"/company/{quote(t)}", status_code=303)

    # ------------------------------------------------------------ assistant

    def agent_for(request: Request, conn) -> Agent:
        cid = request.session.get("chat_id")
        if not cid or cid not in agents:
            cid = uuid.uuid4().hex
            request.session["chat_id"] = cid
            agents[cid] = Agent(llm_client_factory(), lambda: ctx_for(conn),
                                model=os.getenv("NSE_AGENT_MODEL", "claude-sonnet-5-5"))
            while len(agents) > 20:
                agents.popitem(last=False)
        agents.move_to_end(cid)
        agent = agents[cid]
        agent.ctx_factory = lambda: ctx_for(conn)      # this request's connection
        return agent

    def usage_today(conn) -> dict:
        row = conn.execute("SELECT * FROM app_usage WHERE day = %s", (today_fn(),)).fetchone()
        return row or {"chat_questions": 0, "cost_usd": Decimal(0)}

    @app.get("/ask", response_class=HTMLResponse)
    def ask_page(request: Request, _u=Depends(user_dep), conn=Depends(conn_dep)):
        cid = request.session.get("chat_id")
        history = _history(agents[cid].messages) if cid in agents else []
        key_ok = bool(os.getenv("ANTHROPIC_API_KEY")) or llm_client_factory is not make_client
        return render(request, "ask.html", {"history": history, "usage": usage_today(conn),
                                            "limit": settings.chat_daily_limit, "key_ok": key_ok})

    @app.post("/ask", response_class=HTMLResponse, dependencies=[Depends(csrf_dep)])
    def ask(request: Request, question: str = Form(...), _u=Depends(user_dep), conn=Depends(conn_dep)):
        q = question.strip()[:2000]
        if not q:
            return HTMLResponse("")
        used = usage_today(conn)
        if used["chat_questions"] >= settings.chat_daily_limit:
            return render(request, "_exchange.html", {"q": q, "error":
                          f"Today's limit of {settings.chat_daily_limit} questions is used up. "
                          "It resets at midnight (raise CHAT_DAILY_LIMIT to change it)."})
        try:
            agent = agent_for(request, conn)
            before = agent.usage.cost_usd(agent.model) or 0
            turn = agent.ask(q)
            cost = (agent.usage.cost_usd(agent.model) or 0) - before
        except MissingApiKey as exc:
            return render(request, "_exchange.html", {"q": q, "error": str(exc)})
        except Exception as exc:
            conn.rollback()
            return render(request, "_exchange.html", {"q": q, "error": describe_api_error(exc)})
        conn.execute("""INSERT INTO app_usage(day, chat_questions, cost_usd) VALUES (%s, 1, %s)
                        ON CONFLICT (day) DO UPDATE SET chat_questions = app_usage.chat_questions + 1,
                            cost_usd = app_usage.cost_usd + EXCLUDED.cost_usd""",
                     (today_fn(), Decimal(str(round(cost, 6)))))
        tools_used = list(dict.fromkeys(turn.tools_used))
        if not request.headers.get("HX-Request"):          # plain form post (no JavaScript)
            return RedirectResponse("/ask", status_code=303)
        return render(request, "_exchange.html", {"q": q, "answer": turn.text, "tools": tools_used,
                                                  "cost": cost})

    @app.post("/ask/new", dependencies=[Depends(csrf_dep)])
    def ask_new(request: Request, _u=Depends(user_dep)):
        cid = request.session.pop("chat_id", None)
        agents.pop(cid, None)
        return RedirectResponse("/ask", status_code=303)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        detail = getattr(exc, "detail", None) or "That page doesn't exist."
        return templates.TemplateResponse(request, "error.html", {"title": "Not found", "detail": detail,
                                                                 "flashes": [], "path": ""}, status_code=404)

    @app.exception_handler(403)
    async def forbidden(request: Request, exc):
        return templates.TemplateResponse(request, "error.html", {"title": "Not allowed",
                                                                 "detail": getattr(exc, "detail", ""),
                                                                 "flashes": [], "path": ""}, status_code=403)

    return app


# ---------------------------------------------------------------- helpers


def _safe_next(target: str | None) -> str:
    """Only allow local paths after login (no open redirects)."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return "/"
    return target


def _same_site(request: Request, url: str) -> bool:
    return url.startswith(str(request.base_url))


def _dec(text: str, name: str) -> Decimal:
    try:
        return Decimal(text.replace(",", "").strip())
    except InvalidOperation:
        raise ValueError(f"{name} must be a number.")


def _history(messages: list[dict]) -> list[dict]:
    """Rebuild question/answer pairs from an agent's message list for display."""
    out: list[dict] = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], str):
            out.append({"q": m["content"], "answer": "", "tools": []})
        elif m["role"] == "assistant" and out:
            for b in m["content"]:
                btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                if btype == "text":
                    text = b.get("text") if isinstance(b, dict) else b.text
                    out[-1]["answer"] = (out[-1]["answer"] + "\n" + text).strip()
                elif btype == "tool_use":
                    name = b.get("name") if isinstance(b, dict) else b.name
                    out[-1]["tools"].append(name)
    for o in out:
        o["tools"] = list(dict.fromkeys(o["tools"]))
    return out


class _LazyApp:
    """Defer building the app until the server starts (so importing this module
    in tests or the CLI doesn't require web settings)."""

    def __init__(self):
        self._app = None

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app = _LazyApp()
