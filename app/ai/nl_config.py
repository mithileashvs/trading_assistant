"""
Natural-Language Configuration (section 31).

Translates a plain-English instruction into a structured config
proposal that must pass pydantic validation before it can be applied
— never arbitrary code, never a direct trading action. This is the
concrete mechanism behind section 30's "the AI should translate this
into structured configuration" and section 39's "AI cannot override
deterministic safety controls."

SAFETY BOUNDARY: `trading_mode` and `kill_switch` are permanently
excluded from what this translator can touch. Switching to LIVE or
clearing an active kill switch must always go through their own
explicit, human-operated paths (Settings file edit / KillSwitch.deactivate()
with a named actor) — never through a natural-language instruction,
however phrased. This is enforced by ALLOWED_FIELDS below, not by
hoping the parser never produces those keys.

Two translation paths:
- Rule-based (regex/keyword matching): works with zero dependencies,
  fully deterministic, covers common instructions.
- LLM-assisted: if an LLMClient is configured, asks it to extract the
  SAME structured fields — but the result is still validated against
  ALLOWED_FIELDS and the pydantic schema before use, exactly like the
  rule-based path. An LLM is never trusted to bypass validation.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.ai.llm_client import LLMClient
from app.config.settings import RiskSettings, Settings

# Every field the translator is allowed to propose changing. Anything
# else -- including trading_mode and kill_switch -- is never accepted,
# regardless of what a rule or an LLM produces.
ALLOWED_RISK_FIELDS = {
    "risk_per_trade_pct", "max_daily_loss_pct", "max_weekly_loss_pct",
    "max_open_positions", "max_trades_per_day", "max_consecutive_losses",
    "max_spread_points", "max_slippage_points",
}
ALLOWED_TOP_LEVEL_FIELDS = {
    "strategy_trend_pullback_enabled", "strategy_breakout_enabled", "strategy_mean_reversion_enabled",
}


@dataclass
class ConfigProposal:
    raw_instruction: str
    risk_updates: dict[str, Any] = field(default_factory=dict)
    top_level_updates: dict[str, Any] = field(default_factory=dict)
    explanation: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)  # parts of the instruction that weren't understood

    @property
    def is_empty(self) -> bool:
        return not self.risk_updates and not self.top_level_updates

    def to_dict(self) -> dict:
        return {
            "raw_instruction": self.raw_instruction,
            "risk_updates": self.risk_updates,
            "top_level_updates": self.top_level_updates,
            "explanation": self.explanation,
            "unmapped": self.unmapped,
        }


_STRATEGY_NAMES = {
    "trend pullback": "strategy_trend_pullback_enabled",
    "trend-pullback": "strategy_trend_pullback_enabled",
    "breakout": "strategy_breakout_enabled",
    "mean reversion": "strategy_mean_reversion_enabled",
    "mean-reversion": "strategy_mean_reversion_enabled",
}

_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"risk\s+(?:less than|no more than|at most|max(?:imum)?)?\s*([\d.]+)\s*%\s*per\s*trade", re.I),
     "risk_per_trade_pct", "Set risk per trade to {value}%."),
    (re.compile(r"max(?:imum)?\s+daily\s+loss\s+(?:of|to|at)?\s*([\d.]+)\s*%", re.I),
     "max_daily_loss_pct", "Set max daily loss to {value}%."),
    (re.compile(r"max(?:imum)?\s+weekly\s+loss\s+(?:of|to|at)?\s*([\d.]+)\s*%", re.I),
     "max_weekly_loss_pct", "Set max weekly loss to {value}%."),
    (re.compile(r"max(?:imum)?\s+([\d.]+)\s+trades?\s+per\s+day", re.I),
     "max_trades_per_day", "Set max trades per day to {value}."),
    (re.compile(r"max(?:imum)?\s+([\d.]+)\s+open\s+positions?", re.I),
     "max_open_positions", "Set max open positions to {value}."),
    (re.compile(r"max(?:imum)?\s+([\d.]+)\s+consecutive\s+losses", re.I),
     "max_consecutive_losses", "Set max consecutive losses to {value}."),
    (re.compile(r"max(?:imum)?\s+spread\s+(?:of|to|at)?\s*([\d.]+)\s*points?", re.I),
     "max_spread_points", "Set max spread to {value} points."),
]


def _parse_number(raw: str) -> float:
    value = float(raw)
    return int(value) if value.is_integer() else value


def translate_rule_based(instruction: str) -> ConfigProposal:
    proposal = ConfigProposal(raw_instruction=instruction)

    for pattern, field_name, template in _PATTERNS:
        match = pattern.search(instruction)
        if match:
            value = _parse_number(match.group(1))
            proposal.risk_updates[field_name] = value
            proposal.explanation.append(template.format(value=value))

    for phrase, field_name in _STRATEGY_NAMES.items():
        enable_pattern = re.compile(rf"\benable\b[^.]*\b{re.escape(phrase)}\b", re.I)
        disable_pattern = re.compile(rf"\bdisable\b[^.]*\b{re.escape(phrase)}\b", re.I)
        only_pattern = re.compile(rf"\bonly\s+(?:trade\s+)?(?:use\s+)?\b{re.escape(phrase)}\b", re.I)
        if disable_pattern.search(instruction):
            proposal.top_level_updates[field_name] = False
            proposal.explanation.append(f"Disable {phrase} strategy.")
        elif enable_pattern.search(instruction):
            proposal.top_level_updates[field_name] = True
            proposal.explanation.append(f"Enable {phrase} strategy.")
        elif only_pattern.search(instruction):
            # "only use mean reversion" -> enable that one, disable the other two
            for other_phrase, other_field in _STRATEGY_NAMES.items():
                if other_field == field_name:
                    continue
                proposal.top_level_updates.setdefault(other_field, False)
            proposal.top_level_updates[field_name] = True
            proposal.explanation.append(f"Restrict trading to {phrase} strategy only.")

    if proposal.is_empty:
        proposal.unmapped.append(instruction)

    return proposal


_LLM_EXTRACTION_SYSTEM_PROMPT = """You extract trading-bot configuration changes from a plain-English \
instruction. Respond with ONLY a JSON object, no prose, with this exact shape:
{"risk_updates": {<field>: <number>, ...}, "top_level_updates": {<field>: <bool>, ...}, "unmapped": ["<phrase that couldn't be mapped>", ...]}

Only use these risk_updates field names: risk_per_trade_pct, max_daily_loss_pct, max_weekly_loss_pct, \
max_open_positions, max_trades_per_day, max_consecutive_losses, max_spread_points, max_slippage_points.
Only use these top_level_updates field names: strategy_trend_pullback_enabled, strategy_breakout_enabled, \
strategy_mean_reversion_enabled.
NEVER output any other field name, especially never "trading_mode" or "kill_switch" -- those are never \
configurable through this interface. If the instruction asks for either, put the relevant phrase in "unmapped" instead.
If nothing in the instruction maps to an allowed field, return empty risk_updates/top_level_updates and put \
the whole instruction in "unmapped"."""


def translate_via_llm(instruction: str, llm_client: LLMClient) -> ConfigProposal:
    """LLM-assisted extraction. The result is filtered through the same
    ALLOWED_* field sets as the rule-based path before being trusted —
    an LLM producing "trading_mode" or "kill_switch" (or anything else
    not on the allowlist) has that key silently dropped, not applied."""
    proposal = ConfigProposal(raw_instruction=instruction)
    raw = llm_client.complete(_LLM_EXTRACTION_SYSTEM_PROMPT, instruction)
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        proposal.unmapped.append(instruction)
        return proposal

    for key, value in (data.get("risk_updates") or {}).items():
        if key in ALLOWED_RISK_FIELDS:
            proposal.risk_updates[key] = value
            proposal.explanation.append(f"Set {key} to {value} (LLM-extracted).")
    for key, value in (data.get("top_level_updates") or {}).items():
        if key in ALLOWED_TOP_LEVEL_FIELDS:
            proposal.top_level_updates[key] = bool(value)
            proposal.explanation.append(f"Set {key} to {value} (LLM-extracted).")
    proposal.unmapped.extend(data.get("unmapped") or [])
    if proposal.is_empty and not proposal.unmapped:
        proposal.unmapped.append(instruction)
    return proposal


def translate(instruction: str, llm_client: LLMClient | None = None) -> ConfigProposal:
    """Tries the LLM path first (if a client is configured and
    available), falling back to rule-based extraction on any failure
    or if no LLM is configured. The rule-based path is always
    attempted as well if the LLM path produced nothing usable, so a
    working LLM never makes the deterministic fallback unreachable."""
    if llm_client and llm_client.is_available():
        try:
            proposal = translate_via_llm(instruction, llm_client)
            if not proposal.is_empty:
                return proposal
        except Exception:  # noqa: BLE001 - fall back to rule-based on any LLM failure
            pass
    return translate_rule_based(instruction)


class ConfigValidationError(ValueError):
    pass


def apply_proposal(settings: Settings, proposal: ConfigProposal) -> Settings:
    """Applies a ConfigProposal to a COPY of settings, validated through
    pydantic — an invalid value (out of range, wrong type) raises
    rather than silently corrupting the running config. The original
    `settings` object is never mutated in place."""
    risk_dict = settings.risk.model_dump()
    risk_dict.update(proposal.risk_updates)
    try:
        new_risk = RiskSettings(_env_file=None, **risk_dict)
    except Exception as exc:  # noqa: BLE001
        raise ConfigValidationError(f"Proposed risk settings are invalid: {exc}") from exc

    top_level_dict = {
        "strategy_trend_pullback_enabled": settings.strategy_trend_pullback_enabled,
        "strategy_breakout_enabled": settings.strategy_breakout_enabled,
        "strategy_mean_reversion_enabled": settings.strategy_mean_reversion_enabled,
    }
    top_level_dict.update(proposal.top_level_updates)

    updated = settings.model_copy(update={"risk": new_risk, **top_level_dict})
    return updated
