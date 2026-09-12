"""Concrete proposals, never automatic adoption or authorization creation."""

import json
from dataclasses import replace
from pathlib import Path

from config import load_strategy_config
from data.price_policy import provider_price_basis
from delayed_replay.proxy.policy import ASSUMPTIONS
from delayed_replay.replay_policy import audit_value
from delayed_replay.serialization import JsonObject

from .models import SCOPE, LimitedProxyTrialPlan, implementation_hash


def saved_run_proposal(root):
    report = json.loads((Path(root) / "report.json").read_text())
    # Proposed values are NOT operational defaults. No execution occurs here.
    terms = dict(
        initial_capital="200000",
        lot_size=100,
        max_position_pct="0.1",
        max_positions=1,
        commission_rate="0",
        slippage_pct="0.001",
        reservation_buffer_pct="0.01",
        price_quantum="0.01",
        money_quantum="0.01",
        buy_price_rounding="ceiling",
        sell_price_rounding="floor",
        fee_rounding="ceiling",
        reservation_rounding="ceiling",
        amount_rounding="half_even",
        budget_rounding="floor",
        priority_mode="sell_then_score_desc_instrument",
        proceeds_mode="hold_until_later_decision_session",
        fill_mode="all_or_reject",
        position_mode="single_position_full_exit",
        expiry_mode="explicit_target_session_only",
        cost_model="proportional",
        dividend_policy="excluded",
        basis_evidence_hash=report["daily_capture_hash"],
    )
    rules = dict(
        no_trade="terminal_no_fill",
        volume_excess="reject_instrument_batch",
        missing="wait_without_expiry",
        expiry="explicit_finalization_not_implemented",
        temporary_halt="allow_daily_proxy_without_time_condition",
        time_condition="none",
        volume_unit="execution_shares",
        provider_price_basis=provider_price_basis("jquants"),
        availability="session_phase_not_actual_publication_v1",
        corporate_action="stop_preserve",
        risk="no_new_dd_stop",
        end="mark_without_forced_exit",
    )
    config = replace(
        load_strategy_config(Path(__file__).parents[2] / "config/strategy.yaml"),
        initial_capital=200000,
    )
    candidate = dict(
        candidate_id="baseline",
        buy_atr_multiplier="1.5",
        sell_atr_multiplier="1.5",
        range_score_threshold="70",
        adx_entry_max="25",
    )
    return LimitedProxyTrialPlan(
        JsonObject.from_value(
            dict(
                schema="limited-proxy-plan-v1",
                scope=SCOPE,
                model_hash=implementation_hash(),
                source_identity="stage7b:" + report["daily_source_hash"],
                provenance="saved_jquants",
                packets=report["snapshot_hashes"],
                history_packets=[],
                captures=dict(
                    master=report["master_capture_hash"],
                    daily_source=report["daily_source_hash"],
                    daily_capture=report["daily_capture_hash"],
                    calendar=report["calendar_capture_hash"],
                ),
                references=dict(
                    lot=None,
                    price=dict(
                        basis=provider_price_basis("jquants"),
                        volume_unit="execution_shares",
                        review_scope="provider_contract_and_saved_capture_only",
                    ),
                    halt=dict(coverage="unknown", observations={}),
                    external_price=None,
                ),
                terms=terms,
                rules=rules,
                strategy=audit_value(config),
                candidates=[candidate],
                candidate_id="baseline",
                candidate_supply="predeclared_single_candidate",
                monthly_reference=dict(
                    lookback_months=None,
                    warmup_months=None,
                    minimum_warmup_sessions=None,
                    status="not_selected_for_this_proposal",
                ),
                repetition="continuous_and_split_resume_same_plan",
                assumptions=list(ASSUMPTIONS),
            )
        )
    )
