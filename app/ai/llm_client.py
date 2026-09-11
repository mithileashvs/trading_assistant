"""
LLM client interface for the AI layer (section 30).

The AI layer's deterministic pieces (explain.py, summarize.py's
template output) work with NO LLM client at all — they're built
directly from the structured objects the rest of the system already
produces (Signal, RegimeDecision, TradeValidation, backtest metrics).
An LLM client is only used to optionally rephrase that same factual
content more fluidly, or to help parse a natural-language instruction
in nl_config.py — never to originate facts the deterministic layer
didn't already establish.

HONESTY NOTE: AnthropicLLMClient calls api.anthropic.com (allowed by
this environment's network egress) using the anthropic SDK, but no
ANTHROPIC_API_KEY is configured in this sandbox, so the live call path
has not actually been exercised end-to-end here — only NullLLMClient
and the rule-based fallbacks have been tested. If you configure a real
key, AnthropicLLMClient should work as written against the documented
API, but treat it the same way as RealMT5Client: structurally sound,
not yet verified against the real service from this environment.
"""
from __future__ import annotations

import abc
import os


class LLMClient(abc.ABC):
    @abc.abstractmethod
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        """Returns the model's text response, or raises on failure.
        Callers must treat failure as recoverable (fall back to the
        deterministic/rule-based path) rather than fatal."""
        ...

    @abc.abstractmethod
    def is_available(self) -> bool: ...


class NullLLMClient(LLMClient):
    """No-op client used when no LLM is configured. Every deterministic
    feature in the AI layer must work correctly with this client, since
    it's the default."""

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("No LLM client is configured.")

    def is_available(self) -> bool:
        return False


class AnthropicLLMClient(LLMClient):
    """Wraps the Anthropic API. Requires ANTHROPIC_API_KEY (env var) and
    the `anthropic` package. Never used for anything that could
    originate a trading decision — see module docstring."""

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str | None = None):
        self.model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def is_available(self) -> bool:
        if not self._api_key:
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        if not self._api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        try:
            import anthropic
        except ImportError as exc:
            raise RuntimeError("The 'anthropic' package is not installed.") from exc

        client = anthropic.Anthropic(api_key=self._api_key)
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        return "\n".join(parts)


def build_llm_client(prefer_anthropic: bool = True) -> LLMClient:
    """Best-effort factory: returns a working AnthropicLLMClient if
    credentials/package are available, else NullLLMClient. Never
    raises — callers always get something usable."""
    if prefer_anthropic:
        candidate = AnthropicLLMClient()
        if candidate.is_available():
            return candidate
    return NullLLMClient()
