import pytest

from app.ai.llm_client import LLMClient
from app.ai.nl_config import (
    ConfigValidationError,
    apply_proposal,
    translate,
    translate_rule_based,
    translate_via_llm,
)
from app.config.settings import Settings


def test_risk_per_trade_extracted():
    proposal = translate_rule_based("Only trade gold when risk less than 0.3% per trade.")
    assert proposal.risk_updates["risk_per_trade_pct"] == 0.3


def test_max_daily_loss_extracted_with_to_phrasing():
    proposal = translate_rule_based("Set max daily loss to 1.5%")
    assert proposal.risk_updates["max_daily_loss_pct"] == 1.5


def test_max_trades_per_day_extracted():
    proposal = translate_rule_based("Limit to max 2 trades per day")
    assert proposal.risk_updates["max_trades_per_day"] == 2


def test_disable_strategy_extracted():
    proposal = translate_rule_based("Disable the breakout strategy")
    assert proposal.top_level_updates["strategy_breakout_enabled"] is False


def test_enable_strategy_extracted():
    proposal = translate_rule_based("Please enable mean reversion")
    assert proposal.top_level_updates["strategy_mean_reversion_enabled"] is True


def test_only_use_strategy_disables_others():
    proposal = translate_rule_based("Only use mean reversion")
    assert proposal.top_level_updates["strategy_mean_reversion_enabled"] is True
    assert proposal.top_level_updates["strategy_trend_pullback_enabled"] is False
    assert proposal.top_level_updates["strategy_breakout_enabled"] is False


def test_unmapped_instruction_reported_not_silently_ignored():
    proposal = translate_rule_based("Trade only during full moons.")
    assert proposal.is_empty
    assert proposal.unmapped == ["Trade only during full moons."]


def test_trading_mode_and_kill_switch_are_never_extracted():
    # These words appear, but must never produce a field the translator
    # is allowed to touch -- section 39: AI cannot override safety controls.
    proposal = translate_rule_based("Switch to LIVE trading mode and turn off the kill switch.")
    assert proposal.is_empty
    assert "trading_mode" not in proposal.top_level_updates
    assert "kill_switch" not in proposal.top_level_updates
    assert "kill_switch" not in proposal.risk_updates


def test_apply_proposal_updates_a_copy_not_the_original():
    settings = Settings(_env_file=None)
    proposal = translate_rule_based("risk 0.25% per trade")
    updated = apply_proposal(settings, proposal)
    assert updated.risk.risk_per_trade_pct == 0.25
    assert settings.risk.risk_per_trade_pct != 0.25  # original untouched


def test_apply_proposal_rejects_invalid_value():
    settings = Settings(_env_file=None)
    proposal = translate_rule_based("risk 999% per trade")  # way outside RiskSettings' allowed range
    with pytest.raises(ConfigValidationError):
        apply_proposal(settings, proposal)


def test_apply_proposal_preserves_untouched_fields():
    settings = Settings(_env_file=None)
    proposal = translate_rule_based("Disable the breakout strategy")
    updated = apply_proposal(settings, proposal)
    assert updated.strategy_breakout_enabled is False
    assert updated.strategy_trend_pullback_enabled == settings.strategy_trend_pullback_enabled
    assert updated.risk.risk_per_trade_pct == settings.risk.risk_per_trade_pct


class _FakeJSONLLMClient(LLMClient):
    def __init__(self, response: str):
        self._response = response

    def is_available(self) -> bool:
        return True

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        return self._response


def test_llm_extraction_filters_disallowed_fields():
    fake = _FakeJSONLLMClient(
        '{"risk_updates": {"risk_per_trade_pct": 0.4, "not_a_real_field": 99}, '
        '"top_level_updates": {"strategy_breakout_enabled": false, "trading_mode": "LIVE"}, '
        '"unmapped": []}'
    )
    proposal = translate_via_llm("some instruction", fake)
    assert proposal.risk_updates == {"risk_per_trade_pct": 0.4}
    assert proposal.top_level_updates == {"strategy_breakout_enabled": False}
    assert "not_a_real_field" not in proposal.risk_updates
    assert "trading_mode" not in proposal.top_level_updates


def test_llm_extraction_falls_back_gracefully_on_bad_json():
    fake = _FakeJSONLLMClient("this is not json at all")
    proposal = translate_via_llm("some instruction", fake)
    assert proposal.is_empty
    assert proposal.unmapped == ["some instruction"]


def test_translate_falls_back_to_rule_based_when_llm_unavailable():
    proposal = translate("risk 0.6% per trade", llm_client=None)
    assert proposal.risk_updates["risk_per_trade_pct"] == 0.6


def test_translate_falls_back_to_rule_based_when_llm_raises():
    class _BrokenLLMClient(LLMClient):
        def is_available(self) -> bool:
            return True

        def complete(self, system_prompt: str, user_prompt: str) -> str:
            raise RuntimeError("simulated API failure")

    proposal = translate("risk 0.6% per trade", llm_client=_BrokenLLMClient())
    assert proposal.risk_updates["risk_per_trade_pct"] == 0.6
