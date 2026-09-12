"""Explicit immutable trial proposals and separately supplied owner approvals."""

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import get_type_hints

from config.settings import StrategyConfig
from delayed_replay.proxy.policy import ASSUMPTIONS, DailyOpenProxyPolicy
from delayed_replay.registration_candidate import public_payload
from delayed_replay.serialization import JsonObject, digest, require_hash, require_text
from delayed_replay.signal_adapter import SignalAdapter
from delayed_replay.validation import ReplayContractError
from walkforward.candidates import (
    ExecutableCandidateCatalog,
    ExecutableCandidateDefinition,
)

SCOPE = dict(
    symbol="72030",
    start="2026-05-01",
    end="2026-06-01",
    model_id="daily_open_proxy_v1",
    mode="research_only",
    registration_status="draft_not_registered",
    fill_kind="simulated_fill",
    actual_trade_at=None,
    purpose="historical_saved_data_validation",
)


def implementation_hash():
    root = Path(__file__).parents[2]
    # Pin shared strategy/indicator and serialization semantics as well as
    # this adapter; a code update cannot reuse an old owner authorization.
    files = [
        p
        for directory in (
            "delayed_replay",
            "indicators",
            "strategy",
            "screening",
            "risk",
            "config",
            "backtest",
            "walkforward",
            "data",
        )
        for p in sorted((root / directory).rglob("*.py"))
    ]
    files.append(root / "examples/validate_limited_proxy.py")
    return digest(
        {
            str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files
        }
    )


class LimitedArithmeticPolicy(DailyOpenProxyPolicy):
    """Same numeric contract, independent policy identity; no old Gate invocation."""

    @property
    def sha256(self):
        self.require()
        return digest(
            dict(
                terms=self.terms.to_dict(),
                rules=self.rules.to_dict(),
                scope=SCOPE,
                assumptions=list(ASSUMPTIONS),
            )
        )


@dataclass(frozen=True)
class LimitedProxyTrialPlan:
    payload: JsonObject

    def __post_init__(self):
        p = self.payload.to_dict()
        if (
            set(p)
            != {
                "schema",
                "scope",
                "model_hash",
                "source_identity",
                "provenance",
                "packets",
                "history_packets",
                "captures",
                "references",
                "terms",
                "rules",
                "strategy",
                "candidates",
                "candidate_id",
                "candidate_supply",
                "monthly_reference",
                "repetition",
                "assumptions",
            }
            or p["schema"] != "limited-proxy-plan-v1"
        ):
            raise ReplayContractError("limited_plan_schema")
        if p["scope"] != SCOPE or p["provenance"] not in (
            "saved_jquants",
            "artificial_fixture",
        ):
            raise ReplayContractError("limited_plan_scope")
        if p["repetition"] != "continuous_and_split_resume_same_plan" or p[
            "assumptions"
        ] != list(ASSUMPTIONS):
            raise ReplayContractError("limited_plan_assumptions")
        require_hash(p["model_hash"])
        require_text(p["source_identity"])
        for key in ("packets", "history_packets"):
            if type(p[key]) is not list or len(p[key]) != len(set(p[key])):
                raise ReplayContractError("limited_input_references")
            for h in p[key]:
                require_hash(h)
        if len(p["packets"]) != 2 or set(p["packets"]) & set(p["history_packets"]):
            raise ReplayContractError("limited_two_disjoint_parts_required")
        if set(p["captures"]) != {
            "master",
            "daily_source",
            "daily_capture",
            "calendar",
        }:
            raise ReplayContractError("limited_capture_references")
        for h in p["captures"].values():
            require_hash(h)
        if set(p["references"]) != {"lot", "price", "halt", "external_price"}:
            raise ReplayContractError("limited_reference_schema")
        public_payload(p)

    @property
    def sha256(self):
        return self.payload.sha256

    @property
    def policy(self):
        p = self.payload.to_dict()
        return LimitedArithmeticPolicy(
            None if p["terms"] is None else JsonObject.from_value(p["terms"]),
            None if p["rules"] is None else JsonObject.from_value(p["rules"]),
        )

    def signals(self):
        p = self.payload.to_dict()
        if p["strategy"] is None or not p["candidates"] or p["candidate_id"] is None:
            raise ReplayContractError("limited_strategy_unresolved")
        values = dict(p["strategy"])
        for name, kind in get_type_hints(StrategyConfig).items():
            if kind is float:
                if type(values[name]) not in (str, int):
                    raise ReplayContractError("limited_strategy_number")
                values[name] = float(values[name])
        values["range_score_weights"] = {
            k: float(v) for k, v in values["range_score_weights"].items()
        }
        config = StrategyConfig.from_mapping(values)
        candidate_fields = {
            "candidate_id",
            "buy_atr_multiplier",
            "sell_atr_multiplier",
            "range_score_threshold",
            "adx_entry_max",
        }
        if type(p["candidates"]) is not list or any(
            type(d) is not dict
            or set(d) != candidate_fields
            or any(
                type(d[k]) not in (str, int)
                for k in candidate_fields - {"candidate_id"}
            )
            for d in p["candidates"]
        ):
            raise ReplayContractError("limited_candidate_schema")
        candidates = tuple(
            ExecutableCandidateDefinition(
                d["candidate_id"],
                *(
                    float(d[k])
                    for k in (
                        "buy_atr_multiplier",
                        "sell_atr_multiplier",
                        "range_score_threshold",
                        "adx_entry_max",
                    )
                ),
            )
            for d in p["candidates"]
        )
        signals = SignalAdapter(config, ExecutableCandidateCatalog(candidates))
        signals.config(p["candidate_id"])
        if p["candidate_supply"] != "predeclared_single_candidate":
            raise ReplayContractError("limited_candidate_supply_unsupported")
        a = self.policy.require()
        if (
            config.initial_capital != 200000
            or config.lot_size != 100
            or (
                config.slippage_pct != float(a.slippage_pct)
                or config.commission_rate != float(a.commission_rate)
            )
        ):
            raise ReplayContractError("limited_strategy_execution_terms_mismatch")
        return signals


@dataclass(frozen=True)
class ScopedResearchAuthorization:
    payload: JsonObject

    def __post_init__(self):
        p = self.payload.to_dict()
        if (
            set(p)
            != {"schema", "plan_hash", "status", "approval_reference", "recorded_at"}
            or p["schema"] != "limited-authorization-v1"
        ):
            raise ReplayContractError("limited_authorization_schema")
        require_hash(p["plan_hash"])
        require_text(p["approval_reference"])
        from delayed_replay.serialization import parse_time

        parse_time(p["recorded_at"])
        if p["status"] not in (
            "approved_for_limited_trial",
            "artificial_test_authorization",
        ):
            raise ReplayContractError("limited_authorization_status")
        public_payload(p)

    def require(self, plan):
        a, p = self.payload.to_dict(), plan.payload.to_dict()
        expected = (
            "approved_for_limited_trial"
            if p["provenance"] == "saved_jquants"
            else "artificial_test_authorization"
        )
        if a["plan_hash"] != plan.sha256 or a["status"] != expected:
            raise ReplayContractError("limited_authorization_scope_mismatch")


def warmup_requirements(config):
    return dict(
        sma=config.sma_period,
        atr=config.atr_period,
        adx=2 * config.adx_period,
        normalized_slope=config.sma_period + config.slope_lookback - 1,
        range_window=config.range_window,
        mean_crossings=config.sma_period + config.range_window - 1,
        atr_stability=max(config.atr_period, config.sma_period)
        + config.stability_window
        - 1,
        range_stability=config.range_window + config.stability_window - 1,
        liquidity=config.liquidity_window,
    )
