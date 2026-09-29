"""Phase 3: assistant tools against a real database, and the chat loop with a fake model."""
from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass, field
from datetime import date
from types import SimpleNamespace

import pytest

from nse_agent import cli, loaders
from nse_agent.agent import cli as agent_cli
from nse_agent.agent.chat import Agent, Usage, describe_api_error, price_for
from nse_agent.agent.tools import TOOLS, ToolContext, run_tool
from nse_agent.db import connect
from nse_agent.sources import rss

from .conftest import FIXTURES

TEMPLATES = FIXTURES.parents[1] / "templates"
TODAY = date(2024, 11, 20)


@pytest.fixture()
def world(db, tmp_path):
    """Prices, fundamentals, a dividend, news, a profile and two holdings."""
    assert cli.main(["import-prices", str(FIXTURES / "prices_history.csv")]) == 0
    assert cli.main(["import-financials", str(FIXTURES / "financials.csv")]) == 0
    divs = tmp_path / "d.csv"
    divs.write_text("ticker,action_type,amount_per_share,book_closure_date,payment_date,financial_year\n"
                    "KCB,final_dividend,2.00,2024-11-28,2024-12-15,2023\n")
    assert cli.main(["import-dividends", str(divs)]) == 0
    macro = tmp_path / "m.csv"
    macro.write_text("series_code,date,value\nCBK_CBR,2024-08-06,12.75\nCBK_CBR,2024-10-08,12.00\n")
    assert cli.main(["import-macro", str(macro)]) == 0
    assert cli.main(["profile", "create", "--from-file", str(TEMPLATES / "profile.json")]) == 0
    assert cli.main(["holding", "set", "emergency_cash", "Emergency fund", "240000"]) == 0
    assert cli.main(["buy", "SCOM", "1000", "15.50", "--date", "2024-11-18"]) == 0
    assert cli.main(["buy", "KCB", "100", "38", "--date", "2024-11-18"]) == 0
    with connect(db) as conn:
        items = rss.parse_feed((FIXTURES / "capitalfm.xml").read_bytes(), "capitalfm_business")
        loaders.sync_news_sources(conn, rss.load_feeds(FIXTURES.parents[1] / "nse_agent/data/feeds.json"))
        with loaders.ingestion_run(conn, "news", "capitalfm_business") as run:
            loaders.upsert_news(conn, items, loaders.build_tagger(conn), run)
    return db


@pytest.fixture()
def ctx(world):
    with connect(world) as conn:
        yield ToolContext(conn=conn, profile_id=1, today=TODAY)


def call(ctx, name, **args):
    text, is_error = run_tool(ctx, name, args)
    return json.loads(text), is_error


# ---------------------------------------------------------------- tool definitions


def test_tool_specs_are_valid_and_match_functions():
    names = set()
    for t in TOOLS:
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", t.name) and t.name not in names
        names.add(t.name)
        assert len(t.description) > 60
        schema = t.input_schema
        assert schema["type"] == "object"
        params = list(inspect.signature(t.fn).parameters)[1:]      # skip ctx
        assert set(schema["properties"]) == set(params), t.name
        for req in schema.get("required", []):
            assert inspect.signature(t.fn).parameters[req].default is inspect._empty


# ---------------------------------------------------------------- tools


def test_find_companies(ctx):
    r, err = call(ctx, "find_companies", query="safaricom")
    assert not err and [m["ticker"] for m in r["matches"]] == ["SCOM"]
    r, _ = call(ctx, "find_companies", query="M-Pesa")          # alias
    assert r["matches"][0]["ticker"] == "SCOM"
    r, _ = call(ctx, "find_companies", sector="banking")
    assert {"KCB", "EQTY", "FMLY"} <= {m["ticker"] for m in r["matches"]}


def test_get_quotes(ctx):
    r, err = call(ctx, "get_quotes", tickers=["scom", "KCB", "EQTY"])
    assert not err
    q = {x["ticker"]: x for x in r["quotes"]}
    assert q["SCOM"]["close"] == 14.8 and q["SCOM"]["price_date"] == "2024-11-19"
    assert q["SCOM"]["data_age_days"] == 1 and q["SCOM"]["stale"] is False
    assert "note" in q["SCOM"]                                    # short history
    assert r["no_price_data"] == ["EQTY"] and "ingest-prices" in r["hint"]


def test_unknown_ticker_is_a_tool_error(ctx):
    r, err = call(ctx, "get_quotes", tickers=["NOPE"])
    assert err and "find_companies" in r["error"]


def test_bad_arguments_are_reported_not_raised(ctx):
    r, err = call(ctx, "get_quotes", tickrs=["SCOM"])
    assert err and "Bad arguments" in r["error"]
    r, err = call(ctx, "no_such_tool")
    assert err


def test_price_history(ctx):
    r, err = call(ctx, "get_price_history", ticker="SCOM", days=30)
    assert not err
    assert r["start_close"] == 15.8 and r["end_close"] == 14.8 and r["change_pct"] == -6.33
    assert r["coverage_note"]                                      # only 2 days stored


def test_valuations_and_screen(ctx):
    r, _ = call(ctx, "get_valuations", tickers=["SCOM", "KCB"])
    v = {x["ticker"]: x for x in r["valuations"]}
    assert v["SCOM"]["pe_ratio"] == 9.43 and v["SCOM"]["dividend_yield_pct"] == 8.11
    r, _ = call(ctx, "screen_stocks", min_dividend_yield=5, sort_by="dividend_yield")
    assert [x["ticker"] for x in r["results"]] == ["SCOM"]
    assert "Only 2 companies" in r["warning"]
    r, err = call(ctx, "screen_stocks", sort_by="bogus")
    assert err


def test_search_news_by_ticker_topic_and_text(ctx):
    ctx2 = ToolContext(ctx.conn, 1, date(2026, 9, 24))
    r, _ = call(ctx2, "search_news", ticker="SCBK")
    assert r["articles"][0]["title"].startswith("StanChart") and r["articles"][0]["relevance"] >= 0.8
    r, _ = call(ctx2, "search_news", topic="currency")
    assert any("ATMs" in a["title"] for a in r["articles"])
    r, _ = call(ctx2, "search_news", text="Access Bank")
    assert r["count"] == 1
    assert "untrusted" in r["note"] or "not instructions" in r["note"]


def test_portfolio_plan_and_purchase_check(ctx):
    p, _ = call(ctx, "get_my_portfolio")
    assert {h["ticker"] for h in p["holdings"]} == {"SCOM", "KCB"}
    assert p["other_holdings_by_class"]["emergency_cash"] == 240000
    plan, _ = call(ctx, "get_my_plan")
    assert plan["profile"]["horizon_years"] == 15 and plan["target_mix_pct"]["nse_equities"] > 0
    assert plan["profile"]["answers"]["horizon"] == "In more than 10 years"

    c, err = call(ctx, "check_purchase", ticker="SCOM", amount_kes=20000)
    assert not err
    assert c["shares_affordable"] == 1323        # (20000 - fees) // 14.80
    assert c["after_purchase"]["companies_held"] == 2
    assert any("above their target" in f for f in c["flags"])   # 100% shares vs target
    c, _ = call(ctx, "check_purchase", ticker="SCOM", amount_kes=5)
    assert any("doesn't cover one share" in f for f in c["flags"])


def test_dividends_macro_alerts_status_fees(ctx):
    d, _ = call(ctx, "get_upcoming_dividends", days=30)
    kcb = d["dividends"][0]
    assert kcb["ticker"] == "KCB" and kcb["your_shares"] == 100 and kcb["your_net_after_5pct_wht"] == 190
    m, _ = call(ctx, "get_macro", series=["cbk_cbr"])
    assert m["series"][0]["value"] == 12 and m["series"][0]["previous_value"] == 12.75
    a, err = call(ctx, "get_alerts")
    assert not err and a["count"] == 0
    s, _ = call(ctx, "data_status")
    assert s["summary"]["latest_price_date"] == "2024-11-19" and s["summary"]["companies_with_financials"] == 2
    f, _ = call(ctx, "estimate_trade_cost", amount_kes=10000)
    assert f["total_pct"] == 2.05


def test_no_profile(world):
    with connect(world) as conn:
        c = ToolContext(conn, None, TODAY)
        r, err = call(c, "get_my_plan")
        assert err and "profile create" in r["error"]


# ---------------------------------------------------------------- chat loop with a fake model


def text_block(t):
    return SimpleNamespace(type="text", text=t)


def tool_block(i, name, args):
    return SimpleNamespace(type="tool_use", id=i, name=name, input=args)


def response(blocks, stop="end_turn", **usage):
    u = SimpleNamespace(input_tokens=usage.get("inp", 1000), output_tokens=usage.get("out", 200),
                        cache_creation_input_tokens=usage.get("cw", 0),
                        cache_read_input_tokens=usage.get("cr", 0))
    return SimpleNamespace(content=blocks, stop_reason=stop, usage=u)


@dataclass
class FakeClient:
    script: list
    calls: list = field(default_factory=list)

    def __post_init__(self):
        self.messages = self

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_agent(ctx, script, **kw):
    client = FakeClient(script)
    return Agent(client, lambda: ctx, **kw), client


def test_loop_runs_tools_and_returns_answer(ctx):
    agent, client = make_agent(ctx, [
        response([text_block("Checking."), tool_block("t1", "get_quotes", {"tickers": ["SCOM"]}),
                  tool_block("t2", "get_my_plan", {})], stop="tool_use", cw=3000),
        response([text_block("SCOM closed at KES 14.80 (2024-11-19).")], cr=3000),
    ])
    seen = []
    agent.on_tool = lambda n, a: seen.append(n)
    turn = agent.ask("How is Safaricom doing?")
    assert turn.text == "SCOM closed at KES 14.80 (2024-11-19)."
    assert turn.tools_used == ["get_quotes", "get_my_plan"] == seen

    first = client.calls[0]
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "2024-11-20" in first["system"][0]["text"]
    assert len(first["tools"]) == len(TOOLS)

    results = client.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]
    assert json.loads(results[0]["content"])["quotes"][0]["close"] == 14.8
    assert not results[0]["is_error"]
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "user", "assistant"]
    assert agent.usage.api_calls == 2 and agent.usage.cache_read_tokens == 3000


def test_conversation_continues_with_history(ctx):
    agent, client = make_agent(ctx, [response([text_block("Hi")]), response([text_block("Again")])])
    agent.ask("one")
    agent.ask("two")
    assert [m["role"] for m in client.calls[1]["messages"]] == ["user", "assistant", "user"]
    agent.reset()
    assert agent.messages == []


def test_tool_errors_go_back_to_the_model(ctx):
    agent, client = make_agent(ctx, [
        response([tool_block("t1", "get_quotes", {"tickers": ["NOPE"]})], stop="tool_use"),
        response([text_block("I couldn't find that ticker.")]),
    ])
    agent.ask("price of NOPE?")
    res = client.calls[1]["messages"][-1]["content"][0]
    assert res["is_error"] and "Unknown ticker" in res["content"]


def test_too_many_rounds_stops_cleanly(ctx):
    loop = [response([tool_block(f"t{i}", "data_status", {})], stop="tool_use") for i in range(4)]
    agent, _ = make_agent(ctx, loop, max_tool_rounds=2)
    turn = agent.ask("loop forever")
    assert turn.stopped_early and len(turn.tools_used) == 3
    roles = [m["role"] for m in agent.messages]
    assert roles[-1] == "assistant" and all(a != b for a, b in zip(roles, roles[1:]))


def test_api_error_leaves_history_unchanged(ctx):
    agent, _ = make_agent(ctx, [response([text_block("ok")]), RuntimeError("boom")])
    agent.ask("first")
    with pytest.raises(RuntimeError):
        agent.ask("second")
    assert len(agent.messages) == 2


def test_max_tokens_is_flagged(ctx):
    agent, _ = make_agent(ctx, [response([text_block("partial")], stop="max_tokens")])
    assert "cut off" in agent.ask("long").text


def test_usage_cost():
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, cache_read_tokens=1_000_000,
              cache_write_tokens=0, api_calls=3)
    assert u.cost_usd("claude-sonnet-5-5") == pytest.approx(2 + 1 + 0.2)
    assert price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert u.cost_usd("some-future-model") is None


def test_api_error_messages():
    class E(Exception):
        def __init__(self, status, msg):
            super().__init__(msg)
            self.status_code = status
    assert "API key" in describe_api_error(E(401, "invalid x-api-key"))
    assert "credit" in describe_api_error(E(400, "Your credit balance is too low"))
    assert "busy" in describe_api_error(E(529, "Overloaded"))


# ---------------------------------------------------------------- CLI


def test_ask_and_chat_commands(world, capsys):
    args = SimpleNamespace(question=["how", "am", "I", "doing?"], model=None, profile=None, quiet=False)
    fake = FakeClient([
        response([tool_block("t1", "get_my_portfolio", {})], stop="tool_use"),
        response([text_block("You hold SCOM and KCB.")]),
    ])
    assert agent_cli.cmd_ask(args, client=fake) == 0
    out = capsys.readouterr()
    assert "You hold SCOM and KCB." in out.out
    assert "get my portfolio" in out.err and "API calls" in out.err

    fake = FakeClient([response([text_block("Hello James.")])])
    inputs = iter(["hi", "/cost", "/reset", "/exit"])
    chat_args = SimpleNamespace(model="claude-haiku-4-5", profile=None, quiet=True)
    assert agent_cli.cmd_chat(chat_args, client=fake, input_fn=lambda _: next(inputs)) == 0
    out = capsys.readouterr().out
    assert "assistant> Hello James." in out and "Started a new conversation." in out
    assert fake.calls[0]["model"] == "claude-haiku-4-5"


def test_chat_survives_api_error(world, capsys):
    fake = FakeClient([RuntimeError("Overloaded"), response([text_block("Back.")])])
    inputs = iter(["one", "two", "/exit"])
    args = SimpleNamespace(model=None, profile=None, quiet=True)
    assert agent_cli.cmd_chat(args, client=fake, input_fn=lambda _: next(inputs)) == 0
    out = capsys.readouterr().out
    assert "busy" in out and "assistant> Back." in out


def test_missing_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cli.main(["ask", "hello"]) == 4
    assert "console.anthropic.com" in capsys.readouterr().err
