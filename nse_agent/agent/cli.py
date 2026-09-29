"""`nse-agent chat` and `nse-agent ask`."""
from __future__ import annotations

import os
import sys
from datetime import datetime

from ..config import NAIROBI_TZ, get_settings
from ..db import connect
from .chat import DEFAULT_MODEL, Agent, MissingApiKey, describe_api_error, make_client
from .tools import ToolContext

HELP = "Commands: /reset (new conversation), /cost (usage so far), /exit"


def _profile_id(conn) -> int | None:
    row = conn.execute("SELECT min(id) AS id FROM investor_profiles").fetchone()
    return row["id"]


def _make_agent(args, conn, client=None) -> Agent:
    pid = args.profile if getattr(args, "profile", None) is not None else _profile_id(conn)
    settings = get_settings()

    def ctx_factory() -> ToolContext:
        return ToolContext(conn=conn, profile_id=pid, today=datetime.now(NAIROBI_TZ).date(),
                           stale_after_days=settings.stale_after_days)

    def on_tool(name: str, _args: dict) -> None:
        if not args.quiet:
            print(f"  · {name.replace('_', ' ')}", file=sys.stderr, flush=True)

    return Agent(client or make_client(), ctx_factory,
                 model=args.model or os.getenv("NSE_AGENT_MODEL", DEFAULT_MODEL), on_tool=on_tool)


def _cost_line(agent: Agent) -> str:
    u = agent.usage
    cost = u.cost_usd(agent.model)
    money = f"~${cost:.4f}" if cost is not None else "cost unknown for this model"
    return (f"{u.api_calls} API calls, {u.input_tokens + u.cache_read_tokens + u.cache_write_tokens:,} "
            f"input / {u.output_tokens:,} output tokens ({u.cache_read_tokens:,} from cache): {money}")


def cmd_ask(args, client=None) -> int:
    client = client or make_client()  # fail fast on a missing key
    with connect() as conn:
        agent = _make_agent(args, conn, client)
        try:
            turn = agent.ask(" ".join(args.question))
        except Exception as exc:
            print(f"error: {describe_api_error(exc)}", file=sys.stderr)
            return 5
    print(turn.text)
    if not args.quiet:
        print(f"\n[{_cost_line(agent)}]", file=sys.stderr)
    return 0


def cmd_chat(args, client=None, input_fn=input) -> int:
    client = client or make_client()
    with connect() as conn:
        agent = _make_agent(args, conn, client)
        print(f"NSE assistant ({agent.model}). Ask about prices, news, your portfolio or plan.")
        print(HELP)
        while True:
            try:
                q = input_fn("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not q:
                continue
            if q in ("/exit", "/quit", "exit", "quit"):
                break
            if q == "/reset":
                agent.reset()
                print("Started a new conversation.")
                continue
            if q == "/cost":
                print(_cost_line(agent))
                continue
            try:
                turn = agent.ask(q)
            except KeyboardInterrupt:
                print("\n(cancelled)")
                continue
            except Exception as exc:
                print(f"\nerror: {describe_api_error(exc)}")
                continue
            print(f"\nassistant> {turn.text}")
        print(f"[{_cost_line(agent)}]")
    return 0


def register(sub) -> None:
    for name, func, helptext in [
        ("chat", cmd_chat, "talk to the AI assistant about the market and your portfolio"),
        ("ask", cmd_ask, 'one question, one answer: nse-agent ask "how is my portfolio doing?"'),
    ]:
        p = sub.add_parser(name, help=helptext)
        if name == "ask":
            p.add_argument("question", nargs="+")
        p.add_argument("--model", help=f"Claude model (default {DEFAULT_MODEL}, or NSE_AGENT_MODEL)")
        p.add_argument("--profile", type=int)
        p.add_argument("--quiet", action="store_true", help="don't show lookups and cost")
        p.set_defaults(func=func)


__all__ = ["register", "cmd_ask", "cmd_chat", "MissingApiKey"]
