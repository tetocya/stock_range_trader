"""Artificial fixtures: every number is a test choice, not operational approval."""

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
from delayed_replay_account_helpers import evidence
from delayed_replay_account_helpers import policy as account_policy

from config import load_strategy_config
from config.phase3 import ExecutableSelectionPolicy
from data.price_policy import provider_price_basis
from delayed_replay.market_view import MarketView, OpenSnapshot
from delayed_replay.monthly_selection import MonthlySelection
from delayed_replay.replay_calendar import JST, ReplayCalendar, ReplaySession
from delayed_replay.replay_engine import ReplayPlan, money
from delayed_replay.replay_policy import ReplayPolicy
from delayed_replay.serialization import time_text
from delayed_replay.signal_adapter import SignalAdapter
from delayed_replay.snapshot import PriceObservation, PriceSnapshot
from walkforward import ExecutableOutcomeEvaluator, ProviderCapabilityRegistry
from walkforward.candidates import (
    ExecutableCandidateCatalog,
    ExecutableCandidateDefinition,
)
from walkforward.selection import ExecutableCandidateSelector

WALL = datetime(2025, 1, 1, tzinfo=UTC)


def fixture_plan():
    policy = ReplayPolicy(
        purpose="synthetic_test",
        run_start=date(2024, 6, 1),
        run_end=date(2024, 9, 1),
        lookback_months=1,
        warmup_months=1,
        minimum_warmup_sessions=4,
        selection_mode="independent_symbol_validation",
        no_candidate_mode="disable_entries_keep_positions_and_pending",
        proceeds_mode="release_after_sale_open_at_complete_close",
        missing_input_mode="wait_without_expiry",
        corporate_action_mode="stop_preserve_positions",
        maximum_drawdown="0.9",
    )
    days = [
        date(2024, month, day)
        for month, values in (
            (4, (1, 5, 10, 17, 24, 26, 30)),
            (5, (1, 7, 10, 17, 24, 29, 31)),
            (6, (3, 7, 12, 18, 24, 27, 28)),
            (7, (1, 5, 10, 17, 24, 29, 31)),
            (8, (1, 5, 9, 16, 23, 28, 30)),
            (9, (2,)),
        )
        for day in values
    ]
    sessions = tuple(
        ReplaySession(
            d,
            datetime(d.year, d.month, d.day, 8, tzinfo=JST),
            datetime(d.year, d.month, d.day, 9, tzinfo=JST),
            datetime(d.year, d.month, d.day, 15, tzinfo=JST),
            "Asia/Tokyo",
        )
        for d in days
    )
    config = replace(
        load_strategy_config(Path(__file__).parents[1] / "config/strategy.yaml"),
        sma_period=2,
        atr_period=2,
        adx_period=2,
        range_window=2,
        slope_lookback=2,
        crossing_target=1,
        stability_window=2,
        liquidity_window=2,
        normalized_slope_limit=1,
        adx_score_limit=100,
        stability_cv_limit=10,
        median_trading_value_target=1,
        initial_capital=200000,
        max_position_pct=0.4,
        lot_size=100,
        slippage_pct=0,
        commission_rate=0,
        max_drawdown_stop=0.9,
        range_exit_threshold=0,
        adx_exit_min=1000,
        stop_loss_pct=0.9,
        max_holding_days=6,
    )
    catalog = ExecutableCandidateCatalog(
        (
            ExecutableCandidateDefinition("fast", 0, 0, 0, 100),
            ExecutableCandidateDefinition("slow", 0.2, 0.25, 0, 100),
        )
    )
    selector = ExecutableCandidateSelector(
        ExecutableSelectionPolicy(
            primary_metric=ExecutableSelectionPolicy.EXPECTED_PRIMARY_METRIC,
            tie_breakers=ExecutableSelectionPolicy.EXPECTED_TIE_BREAKERS,
            minimum_traded_symbol_count=1,
            minimum_trading_symbol_ratio=0,
            minimum_total_trade_count=1,
            minimum_finite_sharpe_count=1,
            maximum_drawdown_limit=0.9,
        )
    )
    monthly = MonthlySelection(
        policy,
        ExecutableOutcomeEvaluator(config, ProviderCapabilityRegistry()),
        selector,
        catalog,
    )
    bars, opens = [], []
    for symbol, offset in (("A", 0), ("B", 1)):
        for i, session in enumerate(sessions):
            price = float(round(100 + 10 * np.sin(i * 1.3 + offset), 4))
            raw = price / 2
            bars.append(
                PriceObservation(
                    symbol,
                    session.day,
                    session.close_at,
                    (raw, raw + 1, raw - 1, raw, 100000.0),
                    (price, price + 2, price - 2, price, 100000.0),
                    1.0,
                    0.0,
                    0.0,
                )
            )
            opens.append(
                evidence(
                    symbol,
                    session.day.isoformat(),
                    money(raw),
                    market_available_at=time_text(session.open_at),
                )
            )
    snapshot = PriceSnapshot.create(
        provider="jquants",
        provider_price_basis=provider_price_basis("jquants"),
        source_artifact_sha256="a" * 64,
        data_version="synthetic-1",
        first_observed_at=WALL,
        fetched_at=WALL,
        provider_published_at=None,
        publication_time_unknown_reason="artificial_fixture",
        price_basis_evidence_id="synthetic-only",
        observations=tuple(bars),
    )
    market = MarketView(
        (snapshot,),
        (
            OpenSnapshot(
                tuple(opens),
                WALL,
                WALL,
                "artificial-open-1",
                None,
                "artificial_fixture",
            ),
        ),
    )
    return ReplayPlan(
        "synthetic-stage4",
        "synthetic-source-4",
        "9" * 64,
        policy,
        account_policy(max_position_pct="0.4", max_positions=2),
        ReplayCalendar(sessions),
        market,
        ("A", "B"),
        "d" * 64,
        monthly,
        SignalAdapter(config, catalog),
    )


def reprice(snapshot, **changes):
    values = {
        name: getattr(snapshot, name)
        for name in snapshot.__dataclass_fields__
        if name != "payload_sha256"
    }
    values.update(changes)
    return PriceSnapshot.create(**values)


def shorten(plan, end):
    policy = replace(plan.policy, run_end=end)
    return replace(plan, policy=policy, monthly=replace(plan.monthly, policy=policy))
