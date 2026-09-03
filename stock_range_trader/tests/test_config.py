"""Tests for the public YAML configuration contract."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from backtest import BacktestEngine, MarketOnNextOpen
from config import StrategyConfig, load_strategy_config
from risk import RiskManager
from screening import RangeDetector, RangeScorer, RangeScoreWeights
from strategy import MeanReversionStrategy

CONFIG_PATH = Path(__file__).parents[1] / "config" / "strategy.yaml"


def _config() -> dict[str, object]:
    with CONFIG_PATH.open(encoding="utf-8") as source:
        return yaml.safe_load(source)


def test_configuration_contains_every_runtime_parameter() -> None:
    required = {
        "sma_period",
        "atr_period",
        "atr_method",
        "adx_period",
        "buy_atr_multiplier",
        "sell_atr_multiplier",
        "range_score_threshold",
        "range_exit_threshold",
        "adx_entry_max",
        "adx_exit_min",
        "range_breakdown_days",
        "stop_loss_pct",
        "max_holding_days",
        "range_window",
        "slope_lookback",
        "normalized_slope_limit",
        "adx_score_limit",
        "trend_slope_weight",
        "trend_adx_weight",
        "crossing_target",
        "stability_window",
        "stability_cv_limit",
        "liquidity_window",
        "median_trading_value_target",
        "range_score_weights",
        "initial_capital",
        "max_position_pct",
        "max_positions",
        "lot_size",
        "max_drawdown_stop",
        "slippage_pct",
        "commission_rate",
        "annual_trading_days",
        "risk_free_rate",
    }

    assert required.issubset(_config())


def test_specification_defaults_are_preserved() -> None:
    config = _config()

    assert config["sma_period"] == 20
    assert config["adx_period"] == 14
    assert config["buy_atr_multiplier"] == 1.5
    assert config["sell_atr_multiplier"] == 1.5
    assert config["range_score_threshold"] == 70.0
    assert config["stop_loss_pct"] == 0.05
    assert config["max_holding_days"] == 40
    assert config["initial_capital"] == 1_000_000
    assert config["max_position_pct"] == 0.10
    assert config["lot_size"] == 100
    assert config["slippage_pct"] == 0.001
    assert config["commission_rate"] == 0.0
    assert config["range_window"] == 60
    assert config["median_trading_value_target"] == 100_000_000


def test_all_components_can_be_constructed_from_yaml() -> None:
    config = _config()
    detector = RangeDetector(
        sma_period=config["sma_period"],
        atr_period=config["atr_period"],
        atr_method=config["atr_method"],
        adx_period=config["adx_period"],
        range_window=config["range_window"],
        slope_lookback=config["slope_lookback"],
    )
    weights = RangeScoreWeights(**config["range_score_weights"])
    scorer = RangeScorer(
        weights=weights,
        normalized_slope_limit=config["normalized_slope_limit"],
        adx_score_limit=config["adx_score_limit"],
        trend_slope_weight=config["trend_slope_weight"],
        trend_adx_weight=config["trend_adx_weight"],
        crossing_target=config["crossing_target"],
        stability_window=config["stability_window"],
        stability_cv_limit=config["stability_cv_limit"],
        liquidity_window=config["liquidity_window"],
        median_trading_value_target=config["median_trading_value_target"],
        range_threshold=config["range_score_threshold"],
        adx_max=config["adx_entry_max"],
    )
    strategy = MeanReversionStrategy(
        buy_atr_multiplier=config["buy_atr_multiplier"],
        sell_atr_multiplier=config["sell_atr_multiplier"],
        range_score_threshold=config["range_score_threshold"],
        range_exit_threshold=config["range_exit_threshold"],
        adx_entry_max=config["adx_entry_max"],
        adx_exit_min=config["adx_exit_min"],
        stop_loss_pct=config["stop_loss_pct"],
        max_holding_days=config["max_holding_days"],
        range_breakdown_days=config["range_breakdown_days"],
    )
    execution = MarketOnNextOpen(
        slippage_pct=config["slippage_pct"],
        commission_rate=config["commission_rate"],
    )
    risk = RiskManager(
        max_position_pct=config["max_position_pct"],
        lot_size=config["lot_size"],
        max_positions=config["max_positions"],
        max_drawdown_stop=config["max_drawdown_stop"],
    )

    engine = BacktestEngine(
        strategy=strategy,
        execution_model=execution,
        risk_manager=risk,
        initial_capital=config["initial_capital"],
    )

    assert detector.range_window == 60
    assert scorer.weights == weights
    assert engine.initial_capital == 1_000_000


def test_strict_loader_constructs_typed_configuration() -> None:
    config = load_strategy_config(CONFIG_PATH)

    assert isinstance(config, StrategyConfig)
    assert isinstance(config.range_score_weights, RangeScoreWeights)
    assert config.create_engine().initial_capital == 1_000_000


def test_strict_loader_rejects_unknown_keys(tmp_path: Path) -> None:
    values = _config()
    values["misspelled_parameter"] = 1
    path = tmp_path / "invalid.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")

    try:
        load_strategy_config(path)
    except ValueError as error:
        assert "Unknown strategy configuration keys" in str(error)
    else:
        raise AssertionError("unknown YAML key was not rejected")


def test_strict_loader_rejects_retired_average_volume_setting(
    tmp_path: Path,
) -> None:
    values = _config()
    values["average_volume_target"] = values.pop("median_trading_value_target")
    path = tmp_path / "retired.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="average_volume_target was retired.*median_trading_value_target",
    ):
        load_strategy_config(path)
