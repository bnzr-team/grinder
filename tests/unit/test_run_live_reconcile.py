"""Tests for run_live_reconcile env var loading (LC-18 operationalization)."""

from __future__ import annotations

import os

# Import from scripts module
import sys
from decimal import Decimal
from unittest.mock import patch

import pytest

sys.path.insert(0, str(__file__).replace("/tests/unit/test_run_live_reconcile.py", "/scripts"))

from run_live_reconcile import (  # type: ignore[import-not-found]
    ConfigError,
    load_reconcile_config_from_env,
    validate_safety_requirements,
)

from grinder.reconcile.config import RemediationAction, RemediationMode


class TestLoadReconcileConfigFromEnv:
    """Test load_reconcile_config_from_env() function."""

    def test_defaults_when_no_env_set(self) -> None:
        """Test safe defaults when no env vars are set."""
        with patch.dict(os.environ, {}, clear=True):
            config = load_reconcile_config_from_env()

        assert config.remediation_mode == RemediationMode.DETECT_ONLY
        assert config.action == RemediationAction.NONE
        assert config.dry_run is True
        assert config.allow_active_remediation is False
        assert config.remediation_strategy_allowlist == set()
        assert config.remediation_symbol_allowlist == set()
        assert config.max_calls_per_day == 100
        assert config.max_notional_per_day == Decimal("5000")
        assert config.max_calls_per_run == 10
        assert config.max_notional_per_run == Decimal("1000")
        assert config.flatten_max_notional_per_call == Decimal("500")
        assert config.budget_state_path is None

    def test_detect_only_mode(self) -> None:
        """Test DETECT_ONLY mode."""
        with patch.dict(os.environ, {"REMEDIATION_MODE": "detect_only"}, clear=True):
            config = load_reconcile_config_from_env()

        assert config.remediation_mode == RemediationMode.DETECT_ONLY
        assert config.action == RemediationAction.NONE
        assert config.dry_run is True

    def test_plan_only_mode(self) -> None:
        """Test PLAN_ONLY mode."""
        with patch.dict(os.environ, {"REMEDIATION_MODE": "plan_only"}, clear=True):
            config = load_reconcile_config_from_env()

        assert config.remediation_mode == RemediationMode.PLAN_ONLY
        assert config.action == RemediationAction.NONE
        assert config.dry_run is True

    def test_blocked_mode(self) -> None:
        """Test BLOCKED mode."""
        with patch.dict(os.environ, {"REMEDIATION_MODE": "blocked"}, clear=True):
            config = load_reconcile_config_from_env()

        assert config.remediation_mode == RemediationMode.BLOCKED
        assert config.action == RemediationAction.NONE
        assert config.dry_run is True

    def test_execute_cancel_all_mode(self) -> None:
        """Test EXECUTE_CANCEL_ALL mode sets action and dry_run correctly."""
        with patch.dict(os.environ, {"REMEDIATION_MODE": "execute_cancel_all"}, clear=True):
            config = load_reconcile_config_from_env()

        assert config.remediation_mode == RemediationMode.EXECUTE_CANCEL_ALL
        assert config.action == RemediationAction.CANCEL_ALL
        assert config.dry_run is False
        assert config.allow_active_remediation is True

    def test_execute_flatten_mode(self) -> None:
        """Test EXECUTE_FLATTEN mode sets action and dry_run correctly."""
        with patch.dict(os.environ, {"REMEDIATION_MODE": "execute_flatten"}, clear=True):
            config = load_reconcile_config_from_env()

        assert config.remediation_mode == RemediationMode.EXECUTE_FLATTEN
        assert config.action == RemediationAction.FLATTEN
        assert config.dry_run is False
        assert config.allow_active_remediation is True

    def test_invalid_mode_raises_config_error(self) -> None:
        """Test that invalid REMEDIATION_MODE raises ConfigError."""
        with (
            patch.dict(os.environ, {"REMEDIATION_MODE": "invalid_mode"}, clear=True),
            pytest.raises(ConfigError) as exc_info,
        ):
            load_reconcile_config_from_env()

        assert "Invalid REMEDIATION_MODE" in str(exc_info.value)

    def test_strategy_allowlist_csv_parsing(self) -> None:
        """Test CSV parsing for strategy allowlist."""
        with patch.dict(
            os.environ,
            {"REMEDIATION_STRATEGY_ALLOWLIST": "strat_a, strat_b , strat_c"},
            clear=True,
        ):
            config = load_reconcile_config_from_env()

        assert config.remediation_strategy_allowlist == {"strat_a", "strat_b", "strat_c"}

    def test_symbol_allowlist_csv_parsing(self) -> None:
        """Test CSV parsing for symbol allowlist."""
        with patch.dict(
            os.environ,
            {"REMEDIATION_SYMBOL_ALLOWLIST": "BTCUSDT,ETHUSDT"},
            clear=True,
        ):
            config = load_reconcile_config_from_env()

        assert config.remediation_symbol_allowlist == {"BTCUSDT", "ETHUSDT"}

    def test_empty_allowlist_returns_empty_set(self) -> None:
        """Test that empty allowlist env var returns empty set."""
        with patch.dict(
            os.environ,
            {"REMEDIATION_STRATEGY_ALLOWLIST": ""},
            clear=True,
        ):
            config = load_reconcile_config_from_env()

        assert config.remediation_strategy_allowlist == set()

    def test_budget_int_parsing(self) -> None:
        """Test integer budget parsing."""
        with patch.dict(
            os.environ,
            {
                "MAX_CALLS_PER_DAY": "50",
                "MAX_CALLS_PER_RUN": "5",
            },
            clear=True,
        ):
            config = load_reconcile_config_from_env()

        assert config.max_calls_per_day == 50
        assert config.max_calls_per_run == 5

    def test_budget_decimal_parsing(self) -> None:
        """Test decimal budget parsing."""
        with patch.dict(
            os.environ,
            {
                "MAX_NOTIONAL_PER_DAY": "10000.50",
                "MAX_NOTIONAL_PER_RUN": "500.25",
                "FLATTEN_MAX_NOTIONAL_PER_CALL": "100.00",
            },
            clear=True,
        ):
            config = load_reconcile_config_from_env()

        assert config.max_notional_per_day == Decimal("10000.50")
        assert config.max_notional_per_run == Decimal("500.25")
        assert config.flatten_max_notional_per_call == Decimal("100.00")

    def test_invalid_int_raises_config_error(self) -> None:
        """Test that non-integer value raises ConfigError."""
        with (
            patch.dict(os.environ, {"MAX_CALLS_PER_DAY": "not_a_number"}, clear=True),
            pytest.raises(ConfigError) as exc_info,
        ):
            load_reconcile_config_from_env()

        assert "MAX_CALLS_PER_DAY" in str(exc_info.value)

    def test_invalid_decimal_raises_config_error(self) -> None:
        """Test that invalid decimal value raises ConfigError."""
        with (
            patch.dict(os.environ, {"MAX_NOTIONAL_PER_DAY": "not_a_decimal"}, clear=True),
            pytest.raises(ConfigError) as exc_info,
        ):
            load_reconcile_config_from_env()

        assert "MAX_NOTIONAL_PER_DAY" in str(exc_info.value)

    def test_budget_state_path(self) -> None:
        """Test budget state path parsing."""
        with patch.dict(
            os.environ,
            {"BUDGET_STATE_PATH": "/var/lib/grinder/budget.json"},
            clear=True,
        ):
            config = load_reconcile_config_from_env()

        assert config.budget_state_path == "/var/lib/grinder/budget.json"

    def test_budget_state_path_empty_returns_none(self) -> None:
        """Test that empty budget state path returns None."""
        with patch.dict(os.environ, {"BUDGET_STATE_PATH": ""}, clear=True):
            config = load_reconcile_config_from_env()

        assert config.budget_state_path is None

    def test_mode_case_insensitive(self) -> None:
        """Test that mode parsing is case-insensitive."""
        with patch.dict(os.environ, {"REMEDIATION_MODE": "PLAN_ONLY"}, clear=True):
            config = load_reconcile_config_from_env()

        assert config.remediation_mode == RemediationMode.PLAN_ONLY


class TestValidateSafetyRequirements:
    """Test validate_safety_requirements() function."""

    def test_detect_only_does_not_require_mainnet_trade(self) -> None:
        """Test that DETECT_ONLY mode doesn't require ALLOW_MAINNET_TRADE."""
        with patch.dict(os.environ, {"REMEDIATION_MODE": "detect_only"}, clear=True):
            config = load_reconcile_config_from_env()
            # Should not raise
            validate_safety_requirements(config)

    def test_plan_only_does_not_require_mainnet_trade(self) -> None:
        """Test that PLAN_ONLY mode doesn't require ALLOW_MAINNET_TRADE."""
        with patch.dict(os.environ, {"REMEDIATION_MODE": "plan_only"}, clear=True):
            config = load_reconcile_config_from_env()
            # Should not raise
            validate_safety_requirements(config)

    def test_blocked_does_not_require_mainnet_trade(self) -> None:
        """Test that BLOCKED mode doesn't require ALLOW_MAINNET_TRADE."""
        with patch.dict(os.environ, {"REMEDIATION_MODE": "blocked"}, clear=True):
            config = load_reconcile_config_from_env()
            # Should not raise
            validate_safety_requirements(config)

    def test_execute_cancel_requires_mainnet_trade(self) -> None:
        """Test that EXECUTE_CANCEL_ALL requires ALLOW_MAINNET_TRADE=1."""
        with patch.dict(os.environ, {"REMEDIATION_MODE": "execute_cancel_all"}, clear=True):
            config = load_reconcile_config_from_env()
            with pytest.raises(ConfigError) as exc_info:
                validate_safety_requirements(config)

        assert "ALLOW_MAINNET_TRADE=1" in str(exc_info.value)

    def test_execute_flatten_requires_mainnet_trade(self) -> None:
        """Test that EXECUTE_FLATTEN requires ALLOW_MAINNET_TRADE=1."""
        with patch.dict(os.environ, {"REMEDIATION_MODE": "execute_flatten"}, clear=True):
            config = load_reconcile_config_from_env()
            with pytest.raises(ConfigError) as exc_info:
                validate_safety_requirements(config)

        assert "ALLOW_MAINNET_TRADE=1" in str(exc_info.value)

    def test_execute_cancel_passes_with_mainnet_trade_1(self) -> None:
        """Test that EXECUTE_CANCEL_ALL passes with ALLOW_MAINNET_TRADE=1."""
        with patch.dict(
            os.environ,
            {"REMEDIATION_MODE": "execute_cancel_all", "ALLOW_MAINNET_TRADE": "1"},
            clear=True,
        ):
            config = load_reconcile_config_from_env()
            # Should not raise
            validate_safety_requirements(config)

    def test_execute_flatten_passes_with_mainnet_trade_1(self) -> None:
        """Test that EXECUTE_FLATTEN passes with ALLOW_MAINNET_TRADE=1."""
        with patch.dict(
            os.environ,
            {"REMEDIATION_MODE": "execute_flatten", "ALLOW_MAINNET_TRADE": "1"},
            clear=True,
        ):
            config = load_reconcile_config_from_env()
            # Should not raise
            validate_safety_requirements(config)

    def test_execute_flatten_requires_exact_1(self) -> None:
        """Test that ALLOW_MAINNET_TRADE='true' is NOT accepted (must be '1')."""
        with patch.dict(
            os.environ,
            {"REMEDIATION_MODE": "execute_flatten", "ALLOW_MAINNET_TRADE": "true"},
            clear=True,
        ):
            config = load_reconcile_config_from_env()
            with pytest.raises(ConfigError) as exc_info:
                validate_safety_requirements(config)

        assert "ALLOW_MAINNET_TRADE=1" in str(exc_info.value)


class TestFullConfigScenarios:
    """Integration tests for full config scenarios."""

    def test_stage_d_scenario(self) -> None:
        """Test Stage D: EXECUTE_CANCEL_ALL with strict limits."""
        env = {
            "REMEDIATION_MODE": "execute_cancel_all",
            "REMEDIATION_STRATEGY_ALLOWLIST": "default",
            "REMEDIATION_SYMBOL_ALLOWLIST": "BTCUSDT",
            "MAX_CALLS_PER_DAY": "10",
            "MAX_NOTIONAL_PER_DAY": "1000",
            "MAX_CALLS_PER_RUN": "2",
            "MAX_NOTIONAL_PER_RUN": "200",
            "BUDGET_STATE_PATH": "/tmp/budget.json",
            "ALLOW_MAINNET_TRADE": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_reconcile_config_from_env()
            validate_safety_requirements(config)

        assert config.remediation_mode == RemediationMode.EXECUTE_CANCEL_ALL
        assert config.action == RemediationAction.CANCEL_ALL
        assert config.remediation_strategy_allowlist == {"default"}
        assert config.remediation_symbol_allowlist == {"BTCUSDT"}
        assert config.max_calls_per_day == 10
        assert config.max_notional_per_day == Decimal("1000")
        assert config.budget_state_path == "/tmp/budget.json"

    def test_stage_e_scenario(self) -> None:
        """Test Stage E: EXECUTE_FLATTEN with notional caps."""
        env = {
            "REMEDIATION_MODE": "execute_flatten",
            "REMEDIATION_SYMBOL_ALLOWLIST": "BTCUSDT,ETHUSDT",
            "MAX_NOTIONAL_PER_DAY": "5000",
            "FLATTEN_MAX_NOTIONAL_PER_CALL": "100",
            "ALLOW_MAINNET_TRADE": "1",
        }
        with patch.dict(os.environ, env, clear=True):
            config = load_reconcile_config_from_env()
            validate_safety_requirements(config)

        assert config.remediation_mode == RemediationMode.EXECUTE_FLATTEN
        assert config.action == RemediationAction.FLATTEN
        assert config.remediation_symbol_allowlist == {"BTCUSDT", "ETHUSDT"}
        assert config.flatten_max_notional_per_call == Decimal("100")
