"""The chat loop: send the question to Claude, run the tools it asks for, repeat.

The model client is injected, so tests use a scripted fake and no API key.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from .prompt import system_prompt
from .tools import TOOLS, ToolContext, run_tool

DEFAULT_MODEL = "claude-sonnet-5-5"

# USD per million tokens (input, output), from platform.claude.com/docs (Sept 2026).
# Cache writes cost 1.25x input, cache reads 0.1x input.
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-opus-5-5": (4.0, 20.0),
    "claude-fable-5-1": (10.0, 50.0),
}


def price_for(model: str) -> tuple[float, float] | None:
    for prefix, p in PRICES.items():
        if model.startswith(prefix):
            return p
    return None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    api_calls: int = 0

    def add(self, u: Any) -> None:
        self.api_calls += 1
        self.input_tokens += getattr(u, "input_tokens", 0) or 0
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        self.cache_write_tokens += getattr(u, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(u, "cache_read_input_tokens", 0) or 0

    def cost_usd(self, model: str) -> float | None:
        p = price_for(model)
        if p is None:
            return None
        pin, pout = p
        return (self.input_tokens * pin + self.cache_write_tokens * pin * 1.25
                + self.cache_read_tokens * pin * 0.1 + self.output_tokens * pout) / 1_000_000


@dataclass
class Turn:
    text: str
    tools_used: list[str] = field(default_factory=list)
    stopped_early: bool = False


class MissingApiKey(RuntimeError):
    pass


def describe_api_error(exc: Exception) -> str:
    """Turn an Anthropic API error into a short, actionable message."""
    status = getattr(exc, "status_code", None)
    text = str(exc)
    if status == 401:
        return "The API key was rejected. Check ANTHROPIC_API_KEY in your .env file."
    if status == 400 and "credit" in text.lower():
        return "Your API credit balance is too low. Add credits at console.anthropic.com → Billing."
    if status == 404 and "model" in text.lower():
        return "Unknown model name. Check NSE_AGENT_MODEL in .env (e.g. claude-sonnet-5-5)."
    if status == 429:
        return "Rate limited by the API. Wait a minute and try again."
    if status in (500, 529) or "overloaded" in text.lower():
        return "The API is busy right now. Try again shortly."
    if exc.__class__.__name__ in ("APIConnectionError", "APITimeoutError"):
        return "Couldn't reach the Anthropic API. Check your internet connection."
    return f"{exc.__class__.__name__}: {text[:300]}"


def make_client():
    """Real Anthropic client, or a clear error if the key/SDK is missing."""
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise MissingApiKey(
            "ANTHROPIC_API_KEY is not set. Create a key at console.anthropic.com → API keys "
            "(API credits are separate from a Claude Pro subscription), then add this line to "
            "your .env file:\n  ANTHROPIC_API_KEY=sk-ant-...")
    try:
        import anthropic
    except ImportError as exc:
        raise MissingApiKey("The anthropic package is missing: pip install -e .") from exc
    return anthropic.Anthropic()


class Agent:
    def __init__(self, client, ctx_factory: Callable[[], ToolContext], *, model: str = DEFAULT_MODEL,
                 max_tokens: int = 4096, max_tool_rounds: int = 10, today: date | None = None,
                 on_tool: Callable[[str, dict], None] | None = None):
        self.client = client
        self.ctx_factory = ctx_factory
        self.model = model
        self.max_tokens = max_tokens
        self.max_tool_rounds = max_tool_rounds
        self.on_tool = on_tool
        self.messages: list[dict] = []
        self.usage = Usage()
        self._today = today

    # Cache the system prompt and tool definitions: they're identical on every
    # call, so after the first request they're billed at a tenth of the price.
    def _system(self, today: date) -> list[dict]:
        return [{"type": "text", "text": system_prompt(today), "cache_control": {"type": "ephemeral"}}]

    @staticmethod
    def _tools() -> list[dict]:
        specs = [t.spec() for t in TOOLS]
        specs[-1] = {**specs[-1], "cache_control": {"type": "ephemeral"}}
        return specs

    @staticmethod
    def _with_history_cache(messages: list[dict]) -> list[dict]:
        """Mark the newest message as a cache breakpoint.

        Each lookup round re-sends the whole conversation. With a breakpoint on
        the latest message, the next round reads everything up to it from the
        cache at 10% of the input price instead of paying full price again.
        Only the copy sent to the API is marked, so there is never more than
        one moving breakpoint (the API allows 4 in total).
        """
        if not messages:
            return messages
        last = dict(messages[-1])
        content = last["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        else:
            content = list(content)
        tail = content[-1]
        tail = dict(tail) if isinstance(tail, dict) else tail.model_dump(exclude_none=True) \
            if hasattr(tail, "model_dump") else dict(vars(tail))
        tail["cache_control"] = {"type": "ephemeral"}
        content[-1] = tail
        last["content"] = content
        return [*messages[:-1], last]

    def reset(self) -> None:
        self.messages.clear()

    def ask(self, question: str) -> Turn:
        start = len(self.messages)
        try:
            return self._ask(question)
        except BaseException:
            del self.messages[start:]  # keep history valid after an API error or Ctrl+C
            raise

    def _ask(self, question: str) -> Turn:
        ctx = self.ctx_factory()
        today = self._today or ctx.today
        self.messages.append({"role": "user", "content": question})
        used: list[str] = []
        for _ in range(self.max_tool_rounds + 1):
            resp = self.client.messages.create(
                model=self.model, max_tokens=self.max_tokens, system=self._system(today),
                tools=self._tools(), messages=self._with_history_cache(self.messages))
            self.usage.add(getattr(resp, "usage", None))
            # Pass the content back unchanged (it may include thinking blocks).
            self.messages.append({"role": "assistant", "content": resp.content})
            calls = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
            if resp.stop_reason != "tool_use" or not calls:
                text = "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
                if resp.stop_reason == "max_tokens":
                    text += "\n\n[answer cut off: ask me to continue]"
                return Turn(text or "(no answer)", used)
            results = []
            for call in calls:
                used.append(call.name)
                if self.on_tool:
                    self.on_tool(call.name, call.input)
                content, is_error = run_tool(ctx, call.name, call.input)
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": content, "is_error": is_error})
            self.messages.append({"role": "user", "content": results})
        # Too many rounds: close the loop cleanly so the history stays valid.
        self.messages.append({"role": "assistant", "content": [
            {"type": "text", "text": "(stopped: too many lookups for one question)"}]})
        return Turn("I needed too many lookups for that one. Could you narrow the question?",
                    used, stopped_early=True)
