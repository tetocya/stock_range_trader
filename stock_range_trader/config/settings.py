"""Strict conversion from YAML values to configured application components."""

from __future__ import annotations

from dataclasses import dataclass, fields
import math
from pathlib import Path
from typing import TypeAlias

import yaml

from backtest import BacktestEngine, MarketOnNextOpen
from risk import RiskManager
from screening import RangeDetector, RangeScorer, RangeScoreWeights
from strategy import MeanReversionStrategy

PathLike: TypeAlias = str | Path


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    """Validated configuration values and factories for Phase 1 components."""

    sma_period: int
    atr_period: int
    atr_method: str
    adx_period: int
    buy_atr_multiplier: float
    sell_atr_multiplier: float
    range_score_threshold: float
    range_exit_threshold: float
    adx_entry_max: float
    adx_exit_min: float
    range_breakdown_days: int
    stop_loss_pct: float
    max_holding_days: int
    range_window: int
    slope_lookback: int
    normalized_slope_limit: float
    adx_score_limit: float
    trend_slope_weight: float
    trend_adx_weight: float
    crossing_target: int
    stability_window: int
    stability_cv_limit: float
    liquidity_window: int
    average_volume_target: float
    range_score_weights: RangeScoreWeights
    initial_capital: float
    max_position_pct: float
    max_positions: int
    lot_size: int
    max_drawdown_stop: float
    slippage_pct: float
    commission_rate: float
    annual_trading_days: int
    risk_free_rate: float

    def __post_init__(self) -> None:
        if (
            isinstance(self.initial_capital, bool)
            or not math.isfinite(self.initial_capital)
            or self.initial_capital <= 0.0
        ):
            raise ValueError("initial_capital must be finite and greater than zero")
        if (
            isinstance(self.annual_trading_days, bool)
            or not isinstance(self.annual_trading_days, int)
            or self.annual_trading_days <= 0
        ):
            raise ValueError("annual_trading_days must be a positive integer")
        if (
            isinstance(self.risk_free_rate, bool)
            or not math.isfinite(self.risk_free_rate)
            or self.risk_free_rate <= -1.0
        ):
            raise ValueError("risk_free_rate must be finite and greater than -1")

        # Reuse each domain object's validation so YAML and direct Python
        # construction obey exactly the same constraints.
        self.create_detector()
        self.create_scorer()
        self.create_strategy()
        self.create_execution_model()
        self.create_risk_manager()

    @classmethod
    def from_mapping(cls, values: object) -> StrategyConfig:
        """Build from a mapping, rejecting missing keys and configuration typos."""

        if not isinstance(values, dict):
            raise ValueError("strategy configuration must be a YAML mapping")
        expected = {field.name for field in fields(cls)}
        supplied = set(values)
        missing = sorted(expected - supplied)
        unknown = sorted(supplied - expected)
        if missing:
            raise ValueError("Missing strategy configuration keys: " + ", ".join(missing))
        if unknown:
            raise ValueError("Unknown strategy configuration keys: " + ", ".join(unknown))

        converted = dict(values)
        raw_weights = converted["range_score_weights"]
        if not isinstance(raw_weights, dict):
            raise ValueError("range_score_weights must be a YAML mapping")
        try:
            converted["range_score_weights"] = RangeScoreWeights(**raw_weights)
            return cls(**converted)
        except TypeError as error:
            raise ValueError(f"Invalid strategy configuration: {error}") from error

    def create_detector(self) -> RangeDetector:
        return RangeDetector(
            sma_period=self.sma_period,
            atr_period=self.atr_period,
            atr_method=self.atr_method,
            adx_period=self.adx_period,
            range_window=self.range_window,
            slope_lookback=self.slope_lookback,
        )

    def create_scorer(self) -> RangeScorer:
        return RangeScorer(
            weights=self.range_score_weights,
            normalized_slope_limit=self.normalized_slope_limit,
            adx_score_limit=self.adx_score_limit,
            trend_slope_weight=self.trend_slope_weight,
            trend_adx_weight=self.trend_adx_weight,
            crossing_target=self.crossing_target,
            stability_window=self.stability_window,
            stability_cv_limit=self.stability_cv_limit,
            liquidity_window=self.liquidity_window,
            average_volume_target=self.average_volume_target,
            range_threshold=self.range_score_threshold,
            adx_max=self.adx_entry_max,
        )

    def create_strategy(self) -> MeanReversionStrategy:
        return MeanReversionStrategy(
            buy_atr_multiplier=self.buy_atr_multiplier,
            sell_atr_multiplier=self.sell_atr_multiplier,
            range_score_threshold=self.range_score_threshold,
            range_exit_threshold=self.range_exit_threshold,
            adx_entry_max=self.adx_entry_max,
            adx_exit_min=self.adx_exit_min,
            stop_loss_pct=self.stop_loss_pct,
            max_holding_days=self.max_holding_days,
            range_breakdown_days=self.range_breakdown_days,
        )

    def create_execution_model(self) -> MarketOnNextOpen:
        return MarketOnNextOpen(
            slippage_pct=self.slippage_pct,
            commission_rate=self.commission_rate,
        )

    def create_risk_manager(self) -> RiskManager:
        return RiskManager(
            max_position_pct=self.max_position_pct,
            lot_size=self.lot_size,
            max_positions=self.max_positions,
            max_drawdown_stop=self.max_drawdown_stop,
        )

    def create_engine(self) -> BacktestEngine:
        return BacktestEngine(
            strategy=self.create_strategy(),
            execution_model=self.create_execution_model(),
            risk_manager=self.create_risk_manager(),
            initial_capital=self.initial_capital,
        )


def load_strategy_config(path: PathLike) -> StrategyConfig:
    """Load one UTF-8 YAML file into a strict StrategyConfig."""

    source_path = Path(path).expanduser()
    if not source_path.is_file():
        raise FileNotFoundError(f"Strategy configuration file not found: {source_path}")
    try:
        with source_path.open(encoding="utf-8") as source:
            values = yaml.safe_load(source)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"Failed to load strategy configuration: {error}") from error
    return StrategyConfig.from_mapping(values)

