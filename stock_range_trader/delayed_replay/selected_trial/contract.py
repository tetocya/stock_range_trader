"""Frozen owner instructions; no market-dependent configuration choices."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from config.settings import StrategyConfig
from data.price_policy import provider_price_basis
from delayed_replay.limited_trial.models import (
    SCOPE,
    LimitedProxyTrialPlan,
    implementation_hash,
)
from delayed_replay.proxy.policy import DailyOpenProxyPolicy
from delayed_replay.replay_policy import audit_value
from delayed_replay.serialization import JsonObject, digest, require_hash, require_text
from delayed_replay.validation import ReplayContractError

SYMBOLS = ("46890", "94320", "94340")
SOURCE_SHA = "135f38b9c6557520b4270895770ac573e0c23fdc023fb0a90b63ed162683580c"
APPROVAL_REFERENCE = (
    "conversation:owner-approved-fixed-baseline-25pct-20attempts-20minutes"
)
OVERRIDES = dict(
    initial_capital=200000,
    lot_size=100,
    max_position_pct=0.25,
    max_positions=1,
    commission_rate=0.001,
    slippage_pct=0.001,
)


def price_contract_hash():
    return digest(
        dict(
            provider="jquants",
            basis=provider_price_basis("jquants"),
            source="data.price_policy",
            scope="provider_contract_not_symbol_evidence",
        )
    )


def freeze_strategy(source):
    """Copy only the expressly approved source bytes, never a later config."""
    import yaml

    raw = Path(source).read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA:
        raise ReplayContractError("approved_strategy_source_changed")
    values = yaml.safe_load(raw)
    values.update(OVERRIDES)
    return audit_value(StrategyConfig.from_mapping(values))


@dataclass(frozen=True)
class AcquisitionPlan:
    payload: JsonObject

    def __post_init__(self):
        p = self.payload.to_dict()
        if set(p) != {
            "schema",
            "symbols",
            "reference_date",
            "history_start",
            "run_start",
            "run_end",
            "max_attempts",
            "max_seconds",
            "interval_seconds",
            "strategy",
            "strategy_source_sha256",
            "terms",
            "rules",
            "candidate",
            "approval_reference",
            "lot_reviews",
            "model_hash",
            "purpose",
        }:
            raise ReplayContractError("acquisition_plan_fields")
        fixed = dict(
            schema="selected-acquisition-v1",
            symbols=list(SYMBOLS),
            reference_date="2026-04-30",
            history_start="2026-01-01",
            run_start="2026-05-01",
            run_end="2026-06-01",
            max_attempts=20,
            max_seconds=1200,
            interval_seconds=13,
            strategy_source_sha256=SOURCE_SHA,
            purpose="limited_research_only",
        )
        for k, value in fixed.items():
            if type(p[k]) is not type(value) or p[k] != value:
                raise ReplayContractError("acquisition_scope_mismatch")
        require_text(p["approval_reference"])
        require_hash(p["model_hash"])
        if set(p["lot_reviews"]) != set(SYMBOLS):
            raise ReplayContractError("three_lot_reviews_required")
        for h in p["lot_reviews"].values():
            require_hash(h)
        a = DailyOpenProxyPolicy(
            JsonObject.from_value(p["terms"]), JsonObject.from_value(p["rules"])
        ).require()
        if (
            a.max_position_pct,
            a.max_positions,
            a.commission_rate,
            a.slippage_pct,
            a.reservation_buffer_pct,
            a.price_quantum,
            a.money_quantum,
        ) != ("0.25", 1, "0.001", "0.001", "0.01", "0.01", "0.01"):
            raise ReplayContractError("approved_numbers_changed")
        if a.basis_evidence_hash != price_contract_hash():
            raise ReplayContractError("provider_contract_reference_mismatch")
        if (
            a.buy_price_rounding,
            a.sell_price_rounding,
            a.fee_rounding,
            a.reservation_rounding,
            a.amount_rounding,
            a.budget_rounding,
        ) != ("ceiling", "floor", "ceiling", "ceiling", "half_even", "floor"):
            raise ReplayContractError("approved_rounding_changed")
        if p["candidate"] != dict(
            candidate_id="baseline",
            buy_atr_multiplier="1.5",
            sell_atr_multiplier="1.5",
            range_score_threshold="70",
            adx_entry_max="25",
        ):
            raise ReplayContractError("approved_candidate_changed")
        # Pin ALL strategy fields, not only four candidate fields.
        approved_source = (
            Path(__file__).parents[2] / "config/selected_proxy_strategy.yaml"
        )
        if p["strategy"] != freeze_strategy(approved_source):
            raise ReplayContractError("approved_strategy_changed")

    @property
    def sha256(self):
        return self.payload.sha256

    @property
    def policy(self):
        p = self.payload.to_dict()
        return DailyOpenProxyPolicy(
            JsonObject.from_value(p["terms"]), JsonObject.from_value(p["rules"])
        ).require()


@dataclass(frozen=True)
class SelectedPolicy(DailyOpenProxyPolicy):
    scope_value: JsonObject | None = None

    @property
    def sha256(self):
        self.require()
        return digest(
            dict(
                terms=self.terms.to_dict(),
                rules=self.rules.to_dict(),
                scope=self.scope_value.to_dict(),
            )
        )


class SelectedTrialPlan(LimitedProxyTrialPlan):
    _schema = "selected-trial-plan-v1"

    @staticmethod
    def _valid_scope(scope):
        if type(scope) is not dict or set(scope) != set(SCOPE) | {
            "acquisition_hash",
            "selection_hash",
        }:
            return False
        if scope["symbol"] not in SYMBOLS or any(
            scope[k] != v for k, v in SCOPE.items() if k != "symbol"
        ):
            return False
        for key in ("acquisition_hash", "selection_hash"):
            require_hash(scope[key])
        return True

    @property
    def policy(self):
        p = self.payload.to_dict()
        return SelectedPolicy(
            JsonObject.from_value(p["terms"]),
            JsonObject.from_value(p["rules"]),
            JsonObject.from_value(self.scope),
        )


def new_acquisition(template, source, lot_reviews):
    """Called after explicit owner approval; the template file is never updated."""
    p = template.payload.to_dict()
    terms = dict(
        p["terms"],
        max_position_pct="0.25",
        commission_rate="0.001",
        basis_evidence_hash=price_contract_hash(),
    )
    return AcquisitionPlan(
        JsonObject.from_value(
            dict(
                schema="selected-acquisition-v1",
                symbols=list(SYMBOLS),
                reference_date="2026-04-30",
                history_start="2026-01-01",
                run_start="2026-05-01",
                run_end="2026-06-01",
                max_attempts=20,
                max_seconds=1200,
                interval_seconds=13,
                strategy=freeze_strategy(source),
                strategy_source_sha256=SOURCE_SHA,
                terms=terms,
                rules=p["rules"],
                candidate=p["candidates"][0],
                approval_reference=APPROVAL_REFERENCE,
                lot_reviews=lot_reviews,
                model_hash=implementation_hash(),
                purpose="limited_research_only",
            )
        )
    )
