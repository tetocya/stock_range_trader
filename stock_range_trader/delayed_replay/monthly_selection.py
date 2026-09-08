"""Past-only independent-symbol Validation; never an OOS Test Runner."""

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from data.canonical import CANONICAL_COLUMNS, validate_canonical_bars
from walkforward.candidates import ExecutableCandidateCatalog
from walkforward.executable_evaluation import ExecutableOutcomeEvaluator
from walkforward.folds import WalkForwardFold
from walkforward.selection import ExecutableCandidateSelector

from .replay_calendar import shift_month
from .replay_policy import ReplayPolicy, audit_value, fingerprint
from .serialization import JsonObject, time_text
from .validation import ReplayContractError


@dataclass(frozen=True, slots=True)
class SelectionEpoch:
    value: JsonObject

    def __post_init__(self):
        data = self.value.to_dict()
        if data["status"] not in (
            "selected",
            "no_eligible_candidate",
            "insufficient_history",
        ):
            raise ReplayContractError("invalid_selection_epoch_status")
        if (data["candidate_id"] is not None) != (data["status"] == "selected"):
            raise ReplayContractError("invalid_epoch_candidate")


@dataclass(frozen=True, slots=True)
class MonthlySelection:
    policy: ReplayPolicy
    evaluator: ExecutableOutcomeEvaluator
    selector: ExecutableCandidateSelector
    catalog: ExecutableCandidateCatalog

    @property
    def identity(self):
        registry = self.evaluator.capability_registry
        return fingerprint(
            {
                "policy": audit_value(self.policy),
                "validation_config": audit_value(self.evaluator.base_config),
                "selector": audit_value(self.selector.policy),
                "catalog": audit_value(self.catalog),
                "capabilities": [
                    audit_value(registry.get(name)) for name in registry.providers
                ],
            }
        )

    def evaluate(self, bars, boundary, executed_at, universe):
        """No account argument: shared cash, positions and risk cannot leak in."""
        if not isinstance(bars, pd.DataFrame) or not set(CANONICAL_COLUMNS) <= set(
            bars
        ):
            raise ReplayContractError("monthly_canonical_columns_required")
        if (
            not pd.api.types.is_datetime64_any_dtype(bars["date"])
            or bars["date"].isna().any()
        ):
            raise ReplayContractError("monthly_invalid_date_column")
        start = shift_month(boundary, -self.policy.lookback_months)
        warmup = shift_month(start, -self.policy.warmup_months)
        # Slice BEFORE inspecting provider, corporate actions, or symbol cohort.
        visible = bars.loc[
            (bars.date.dt.date >= warmup)
            & (bars.date.dt.date < boundary)
            & bars.symbol.isin(universe),
            list(CANONICAL_COLUMNS),
        ].copy()
        visible = visible.sort_values(["symbol", "date"], kind="stable").reset_index(
            drop=True
        )
        result = dict(
            boundary=boundary.isoformat(),
            executed_at=time_text(executed_at),
            validation_start=start.isoformat(),
            validation_end=boundary.isoformat(),
            warmup_start=warmup.isoformat(),
            configuration_hash=self.identity,
            input_hash=fingerprint(visible.to_dict("records")),
            candidate_id=None,
            status="insufficient_history",
            scores=[],
            outcomes=[],
            cohort=[],
            exclusions=[],
            selection=None,
        )
        if visible.empty:
            result["exclusions"] = [
                {"symbol": s, "reason": "no_history"} for s in sorted(universe)
            ]
            return SelectionEpoch(JsonObject.from_value(result))
        # Invalid data is NOT interpreted as ordinary history insufficiency.
        self.evaluator.capability_registry.require(
            "jquants", "executable_validation", require_benchmark=True
        )
        validate_canonical_bars(visible, expected_provider="jquants")
        eligible = []
        for symbol in sorted(universe):
            history = visible.loc[visible.symbol == symbol]
            warmup_count = (history.date.dt.date < start).sum()
            if warmup_count < self.policy.minimum_warmup_sessions:
                result["exclusions"].append(
                    {"symbol": symbol, "reason": "insufficient_warmup"}
                )
            elif not (history.date.dt.date >= start).any():
                result["exclusions"].append(
                    {"symbol": symbol, "reason": "no_validation_history"}
                )
            else:
                eligible.append(symbol)
        if not eligible:
            return SelectionEpoch(JsonObject.from_value(result))
        # Test dates only satisfy the existing fold structural type. No Test
        # prices are supplied and evaluate_test is never invoked.
        fold = WalkForwardFold(
            "monthly_" + boundary.isoformat(),
            warmup,
            start,
            start,
            boundary,
            boundary,
            boundary + timedelta(days=1),
            0,
        )
        evaluated = self.evaluator.evaluate_validation(
            visible.loc[visible.symbol.isin(eligible)].copy(),
            fold,
            self.catalog,
        )
        selection = self.selector.select(self.catalog, evaluated.scores)
        result.update(
            status=selection.status.value,
            candidate_id=selection.selected_candidate_id,
            scores=audit_value(evaluated.scores),
            outcomes=audit_value(evaluated.symbol_outcomes),
            cohort=sorted({o.symbol for o in evaluated.symbol_outcomes}),
            selection=audit_value(selection),
        )
        result["exclusions"].extend(audit_value(evaluated.symbol_exclusions))
        return SelectionEpoch(JsonObject.from_value(result))
