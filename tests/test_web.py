"""Web app: security, pages and actions, against a real Postgres and a fake model."""
from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from nse_agent import cli
from nse_agent.web.app import WebSettings, _safe_next, create_app
from nse_agent.web.security import LoginThrottle, hash_password, verify_password

from .conftest import FIXTURES
from .test_agent import FakeClient, response, text_block, tool_block

TEMPLATES = FIXTURES.parents[1] / "templates"
PASSWORD = "correct horse battery"
HASH = hash_password(PASSWORD)
TODAY = date(2024, 11, 20)


def q(url, sql, *params):
    with psycopg.connect(url, row_factory=dict_row) as c:
        return c.execute(sql, params or None).fetchall()


def make_client(db, *, fake=None, secure=False, limit=40):
    settings = WebSettings(password_hash=HASH, session_secret="s" * 40, cookie_secure=secure,
                           chat_daily_limit=limit, database_url=db)
    app = create_app(settings, llm_client_factory=(lambda: fake) if fake else (lambda: FakeClient([])),
                     today_fn=lambda: TODAY)
    return TestClient(app, base_url="https://testserver" if secure else "http://testserver")


def csrf_from(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(c, password=PASSWORD):
    tok = csrf_from(c.get("/login").text)
    return c.post("/login", data={"password": password, "csrf_token": tok, "next": "/"}, follow_redirects=False)


@pytest.fixture()
def seeded(db):
    cli.main(["import-prices", str(FIXTURES / "prices_history.csv")])
    cli.main(["import-financials", str(FIXTURES / "financials.csv")])
    cli.main(["profile", "create", "--from-file", str(TEMPLATES / "profile.json")])
    cli.main(["holding", "set", "emergency_cash", "Emergency fund", "240000"])
    cli.main(["buy", "SCOM", "1000", "15.50", "--date", "2024-11-18"])
    cli.main(["alerts", "run", "--date", "2024-11-20"])
    return db


@pytest.fixture()
def client(seeded):
    with make_client(seeded) as c:
        assert login(c).status_code == 303
        yield c


# ---------------------------------------------------------------- unit


def test_password_hashing():
    assert verify_password(PASSWORD, HASH)
    assert not verify_password("wrong password!!", HASH)
    assert not verify_password(PASSWORD, "garbage")
    assert hash_password(PASSWORD) != HASH                      # salted
    with pytest.raises(ValueError):
        hash_password("short")


def test_throttle():
    now = [0.0]
    t = LoginThrottle(max_failures=3, window=60, clock=lambda: now[0])
    for _ in range(3):
        assert not t.blocked("ip")
        t.fail("ip")
    assert t.blocked("ip") > 0
    now[0] = 61
    assert not t.blocked("ip")


@pytest.mark.parametrize("target,expected", [
    ("/plan", "/plan"), ("//evil.com", "/"), ("https://evil.com", "/"), ("/\\evil", "/"), ("", "/")])
def test_safe_next(target, expected):
    assert _safe_next(target) == expected


def test_settings_require_secrets(monkeypatch):
    monkeypatch.delenv("APP_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="web-setup"):
        WebSettings.from_env()


# ---------------------------------------------------------------- auth & security


def test_pages_require_login(seeded):
    with make_client(seeded) as c:
        r = c.get("/plan", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login?next=/plan"
        r = c.get("/alerts", headers={"HX-Request": "true"})
        assert r.headers["HX-Redirect"].startswith("/login")
        assert c.get("/healthz").json() == {"ok": True}          # health check stays public


def test_login_flow_and_throttle(seeded):
    with make_client(seeded) as c:
        assert login(c, "wrong password!!").status_code == 401
        r = c.post("/login", data={"password": PASSWORD, "csrf_token": "bogus"}, follow_redirects=False)
        assert r.status_code == 403
        for _ in range(4):
            login(c, "wrong password!!")
        assert login(c).status_code == 429                      # locked out even with the right one
    with make_client(seeded) as c:                              # fresh app: fresh throttle
        r = login(c)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert c.get("/").status_code == 200
        tok = csrf_from(c.get("/").text)
        c.post("/logout", data={"csrf_token": tok})
        assert c.get("/", follow_redirects=False).status_code == 303


def test_secure_cookie_and_headers(seeded):
    with make_client(seeded, secure=True) as c:
        r = login(c)
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie
        h = c.get("/").headers
        assert "script-src 'self'" in h["content-security-policy"] and "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY" and h["cache-control"] == "no-store"
        assert "max-age" in h["strict-transport-security"]


def test_post_without_csrf_is_rejected(client, seeded):
    r = client.post("/trades", data={"side": "buy", "ticker": "KCB", "quantity": "10", "price": "39"})
    assert r.status_code == 403
    assert q(seeded, "SELECT count(*) AS n FROM transactions")[0]["n"] == 1


def test_no_inline_scripts_or_styles(client):
    """The CSP forbids inline code; rendered pages must not rely on it."""
    for path in ["/", "/market", "/company/SCOM", "/plan", "/alerts", "/news", "/ask", "/login"]:
        html = client.get(path).text
        assert " style=" not in html, path
        for tag in re.findall(r"<script[^>]*>", html):
            assert 'src="/static/' in tag or 'type="application/json"' in tag, (path, tag)
        assert " onclick=" not in html and "javascript:" not in html


# ---------------------------------------------------------------- pages


def test_every_page_renders(client):
    for path, must in [("/", "This month"), ("/market", "SCOM"), ("/market?sector=BANKING&sort=yield", "KCB"),
                       ("/company/SCOM", "Key figures"), ("/plan", "In order of priority"),
                       ("/alerts", "Open"), ("/news", "News"), ("/ask", "Ask")]:
        r = client.get(path)
        assert r.status_code == 200 and must in r.text, path
    assert client.get("/company/NOPE").status_code == 404


def test_overview_numbers_match_portfolio(client):
    html = client.get("/").text
    assert "254,800" in html                       # 240,000 cash + 1,000 x 14.80
    assert "Put KES" in html                       # the month's instruction
    assert 'href="/company/SCOM"' in html


def test_company_page_chart_data(client):
    html = client.get("/company/SCOM").text
    data = re.search(r'<script type="application/json" id="price-data">(.*?)</script>', html, re.S).group(1)
    assert '"2024-11-19"' in data and "14.8" in data
    assert "9.43" in client.get("/company/SCOM").text            # P/E from imported results


def test_empty_states_without_profile(db):
    with make_client(db) as c:
        login(c)
        assert "profile create" in c.get("/").text
        assert "profile create" in c.get("/plan").text


# ---------------------------------------------------------------- actions


def test_record_trade_and_errors(client, seeded):
    tok = csrf_from(client.get("/").text)
    r = client.post("/trades", data={"csrf_token": tok, "side": "buy", "ticker": "kcb", "quantity": "100",
                                     "price": "39.15", "fees": "", "trade_date": "2024-11-19"})
    assert r.status_code == 200 and "Bought 100 KCB" in r.text
    row = q(seeded, "SELECT * FROM transactions WHERE ticker = 'KCB'")[0]
    assert row["fees"] > 0                                       # estimated
    r = client.post("/trades", data={"csrf_token": tok, "side": "sell", "ticker": "SCOM", "quantity": "5000",
                                     "price": "15", "trade_date": "2024-11-20"})
    assert "only 1,000 held" in r.text
    r = client.post("/trades", data={"csrf_token": tok, "side": "buy", "ticker": "NOPE", "quantity": "1", "price": "1"})
    assert "Unknown ticker NOPE" in r.text


def test_alert_done_and_check(client, seeded):
    tok = csrf_from(client.get("/alerts").text)
    aid = q(seeded, "SELECT id FROM alerts ORDER BY id LIMIT 1")[0]["id"]
    r = client.post(f"/alerts/{aid}/done", headers={"HX-Request": "true", "X-CSRF-Token": tok})
    assert r.status_code == 200 and r.text == ""
    assert q(seeded, "SELECT acknowledged_at FROM alerts WHERE id = %s", aid)[0]["acknowledged_at"]
    r = client.post("/alerts/check", data={"csrf_token": tok})
    assert "Checked." in r.text


def test_watchlist(client, seeded):
    tok = csrf_from(client.get("/company/KCB").text)
    r = client.post("/watchlist", data={"csrf_token": tok, "ticker": "KCB", "action": "add"})
    assert "Remove from watchlist" in r.text
    assert q(seeded, "SELECT ticker FROM watchlist")[0]["ticker"] == "KCB"
    client.post("/watchlist", data={"csrf_token": tok, "ticker": "KCB", "action": "remove"})
    assert not q(seeded, "SELECT ticker FROM watchlist")


# ---------------------------------------------------------------- assistant


def test_ask_flow_usage_and_history(seeded):
    fake = FakeClient([
        response([tool_block("t1", "get_my_portfolio", {})], stop="tool_use"),
        response([text_block("You hold 1,000 SCOM worth KES 14,800 (2024-11-19).")]),
    ])
    with make_client(seeded, fake=fake) as c:
        login(c)
        tok = csrf_from(c.get("/ask").text)
        r = c.post("/ask", data={"question": "How am I doing?"},
                   headers={"HX-Request": "true", "X-CSRF-Token": tok})
        assert "You hold 1,000 SCOM" in r.text and "get my portfolio" in r.text
        assert q(seeded, "SELECT chat_questions FROM app_usage")[0]["chat_questions"] == 1
        page = c.get("/ask").text
        assert "How am I doing?" in page and "You hold 1,000 SCOM" in page   # history persists
        c.post("/ask/new", data={"csrf_token": tok})
        assert "How am I doing?" not in c.get("/ask").text


def test_ask_escapes_model_output(seeded):
    fake = FakeClient([response([text_block("<script>alert(1)</script>")])])
    with make_client(seeded, fake=fake) as c:
        login(c)
        tok = csrf_from(c.get("/ask").text)
        r = c.post("/ask", data={"question": "hi"}, headers={"HX-Request": "true", "X-CSRF-Token": tok})
        assert "<script>alert" not in r.text and "&lt;script&gt;" in r.text


def test_ask_daily_limit_and_api_errors(seeded):
    fake = FakeClient([response([text_block("one")]), RuntimeError("Overloaded")])
    with make_client(seeded, fake=fake, limit=2) as c:
        login(c)
        tok = csrf_from(c.get("/ask").text)
        h = {"HX-Request": "true", "X-CSRF-Token": tok}
        assert "one" in c.post("/ask", data={"question": "a"}, headers=h).text
        assert "busy" in c.post("/ask", data={"question": "b"}, headers=h).text
        q_used = q(seeded, "SELECT chat_questions FROM app_usage")[0]["chat_questions"]
        assert q_used == 1                                  # failed calls don't count
        with psycopg.connect(seeded) as conn:
            conn.execute("UPDATE app_usage SET chat_questions = 2")
        assert "limit of 2 questions" in c.post("/ask", data={"question": "c"}, headers=h).text


# ---------------------------------------------------------------- db-copy


def test_db_copy(seeded):
    from nse_agent.dbcopy import copy_database
    with psycopg.connect(seeded, autocommit=True) as c:
        c.execute("DROP DATABASE IF EXISTS copy_target")
        c.execute("CREATE DATABASE copy_target")
    target = re.sub(r"/[^/?]+(\?|$)", r"/copy_target\1", seeded, count=1)
    with psycopg.connect(seeded) as c:            # simulate a database created before Phase 4
        c.execute("DROP TABLE app_usage")
    counts = copy_database(seeded, target, log=lambda *_: None)
    assert counts["transactions"] == 1 and counts["companies"] == 73 and counts["daily_prices"] == 3
    with pytest.raises(RuntimeError, match="--replace"):
        copy_database(seeded, target, log=lambda *_: None)
    copy_database(seeded, target, replace=True, log=lambda *_: None)
    with pytest.raises(RuntimeError, match="same database"):
        copy_database(target, target, replace=True, log=lambda *_: None)
    with psycopg.connect(target, row_factory=dict_row) as c:     # json columns survive the trip
        prof = c.execute("SELECT questionnaire FROM investor_profiles").fetchone()
        assert prof["questionnaire"]["horizon"] == 4
    # sequences continue after copied ids
    with psycopg.connect(target) as c:
        new_id = c.execute("INSERT INTO investor_profiles(display_name) VALUES ('x') RETURNING id").fetchone()[0]
        assert new_id == 2
    with psycopg.connect(seeded, autocommit=True) as c:
        c.execute("DROP DATABASE copy_target WITH (FORCE)")
