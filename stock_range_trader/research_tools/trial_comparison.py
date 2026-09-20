"""Saved independent research trials, never a portfolio or performance selector."""

from collections import Counter
from dataclasses import dataclass
from decimal import localcontext
from itertools import combinations
from pathlib import Path

from delayed_replay.june_trial import JunePlan
from delayed_replay.serialization import JsonObject, digest, require_hash

from .account_view import AccountReadModel, AccountViewBuilder
from .arithmetic import number, text
from .order_audit import _price_evidence
from .reader import EvidenceFiles, ObservationError

TERMINAL = frozenset(("filled", "rejected", "cancelled", "canceled"))
LIMITATIONS = [
    "simulated_fill_not_actual_execution",
    "actual_trade_time_and_open_liquidity_unverified",
    "halt_information_may_be_unknown",
    "hashes_do_not_prove_authenticity_or_transition_correctness",
    "saved_entry_condition_count_unavailable_no_signal_recalculation",
    "no_equity_linking_compounding_ranking_recommendation_or_formal_oos",
]


def _conditions(plan, account_mode):
    terms, rules, scope = plan["terms"], plan["rules"], plan["scope"]
    return dict(
        symbol=scope.get("symbol"),
        period=[scope.get("start"), scope.get("end")],
        initial_capital=terms.get("initial_capital"),
        lot_size=terms.get("lot_size"),
        max_position_pct=terms.get("max_position_pct"),
        max_positions=terms.get("max_positions"),
        candidate_id=plan.get("candidate_id"),
        candidates=plan.get("candidates"),
        strategy=plan["strategy"],
        settings_hash=digest({k: plan[k] for k in ("strategy", "terms", "rules")}),
        execution_terms=terms,
        proxy_rules=rules,
        provider_price_basis=rules.get("provider_price_basis"),
        dividend_policy=terms.get("dividend_policy"),
        corporate_action=rules.get("corporate_action"),
        model_contract_version=scope.get("model_id"),
        implementation_hash=plan.get("model_hash"),
        account_mode=account_mode,
        provenance=plan.get("provenance"),
    )


def _optional_json(path, files):
    try:
        return files.json(path)
    except ObservationError as exc:
        if str(exc) != "missing_input":
            raise
        return None


def _saved_comparison(root, plan_hash, files):
    obj = _optional_json(root / "comparison/comparison.json", files)
    if obj is None:
        return dict(status="unverified", reason="no_saved_comparison")
    p = obj.to_dict()
    if p.get("schema") not in ("limited-comparison-v1", "june-comparison-v1"):
        raise ObservationError("unsupported_saved_comparison_schema")
    if p.get("plan_hash") != plan_hash:
        raise ObservationError("comparison_plan_mismatch")
    # A saved assertion is not a rerun, nor proof against both current ledgers.
    if p["schema"] == "limited-comparison-v1":
        equal = p.get("logical_state_equal")
    else:
        matches = p.get("matches")
        if not isinstance(matches, dict) or not matches:
            raise ObservationError("invalid_saved_comparison")
        if any(type(v) is not bool for v in matches.values()):
            raise ObservationError("invalid_saved_comparison")
        equal = all(matches.values())
    if type(equal) is not bool:
        raise ObservationError("invalid_saved_comparison")
    return dict(
        status="saved_assertion_only_not_reexecuted",
        recorded_equal=equal,
        artifact_hash=obj.sha256,
        current_pair_equivalence="unverified",
    )


def _inventory(state, stored, root, files, cache):
    plan = state["identity"]["plan"]
    inputs = state["inputs"]
    declared = plan["history_packets"] + plan["packets"]
    for sha in declared:
        require_hash(sha)
    missing = []
    days = []
    for key, item in sorted(inputs.items()):
        if item["packet"] not in declared:
            raise ObservationError("saved_input_outside_declared_plan")
        row = item["row"]
        symbol, day = row["symbol"], row["session"]
        if key != symbol + "|" + day:
            raise ObservationError("comparison_input_key_mismatch")
        _, _, absent = _price_evidence(root, state, symbol, day, files, cache)
        missing.extend(absent)
        days.append(day)
    start, end = plan["scope"]["start"], plan["scope"]["end"]
    sessions = state["identity"]["run_sessions"]
    missing_sessions = sorted(set(sessions) - set(days))
    initial = stored.initial_state.to_dict()
    # This describes a saved fresh account; it does not infer a continued lineage.
    mode = (
        "fresh_independent_account"
        if initial.get("positions") == {}
        and initial.get("orders") == {}
        and initial.get("cash") == plan["terms"].get("initial_capital")
        else "unverified"
    )
    episodes = state.get("episodes")
    if episodes is not None and not isinstance(episodes, dict):
        raise ObservationError("invalid_saved_episodes")
    return dict(
        conditions=_conditions(plan, mode),
        declared_input_hash=digest(
            dict(
                history_packets=plan.get("history_packets"),
                packets=plan.get("packets"),
                source_identity=plan.get("source_identity"),
            )
        ),
        style=state["identity"].get("style", "unverified"),
        input_validation=dict(
            status="insufficient_evidence" if missing else "saved_rows_checked",
            missing_evidence=sorted(set(missing)),
        ),
        input_counts=dict(
            saved_observations=len(inputs),
            saved_sessions=len(set(days)),
            warmup_observations=sum(d < start for d in days),
            evaluation_observations=sum(start <= d < end for d in days),
            scheduled_sessions=len(sessions),
            absent_scheduled_sessions=len(missing_sessions),
            absent_session_dates=missing_sessions,
            # Not an inferred exchange calendar or proof of complete market data.
            basis="saved_rows_and_saved_run_schedule_only",
        ),
        completed_trades=None if episodes is None else len(episodes),
        saved_comparison=_saved_comparison(root, digest(plan), files),
    )


@dataclass(frozen=True)
class TrialComparisonInput:
    payload: JsonObject
    files: EvidenceFiles
    input_root: Path

    @classmethod
    def read(cls, trial_root, account=None):
        root = Path(trial_root).absolute()
        # Only an explicit acquisition-only plan supports the unacquired lane.
        # A missing ledger in a clearing trial is an error, never an empty account.
        files = EvidenceFiles()
        clearing = [
            _optional_json(root / name, files)
            for name in ("trial_plan.json", "clearing_plan.json")
        ]
        if not any(clearing):
            plan_object = files.json(root / "plan.json")
            JunePlan(plan_object)  # Existing pure schema validation; no inspect/run.
            p = plan_object.to_dict()
            for name in ("input_manifest.json", "comparison/continuous.sqlite"):
                try:
                    files.read(root / name)
                except ObservationError as exc:
                    if str(exc) != "missing_input":
                        raise
                else:
                    raise ObservationError("ambiguous_preparation_with_results")
            if account is not None:
                raise ObservationError("account_not_available_for_preparation")
            normalized = dict(
                **p["settings"],
                scope=p["scope"],
                model_hash=p["implementation_hash"],
                provenance="unacquired",
            )
            data = dict(
                trial_id=plan_object.sha256,
                run_id="unacquired:" + plan_object.sha256,
                declared_input_hash=None,
                provenance="unacquired",
                planned_parent_provenance=p["parent"]["provenance"],
                conditions=_conditions(normalized, "unverified"),
                availability="unacquired",
                style=None,
                metadata=None,
                input_validation=dict(status="unacquired"),
                input_counts=None,
                audit_validation=None,
                metrics=None,
                saved_comparison=dict(status="unverified", reason="unacquired"),
                limitations=LIMITATIONS,
            )
            files.verify()
            return cls(JsonObject.from_value(data), files, root)
        projections = []

        def observe(*args):
            projections.append(_inventory(*args))

        model = AccountReadModel.read(root, account, _observer=observe)
        view = AccountViewBuilder().build(model).to_dict()
        extra = projections[0]
        meta, summary = view["metadata"], view["summary"]
        orders = view["orders"]
        counts = Counter(o["saved_status"] for o in orders)
        terminal = sum(counts[s] for s in TERMINAL)
        with localcontext() as ctx:
            ctx.prec = 128
            rate = (
                text(number(str(counts["filled"])) * 100 / terminal)
                if terminal
                else None
            )
        complete = view["replay"]["status"] == "completed"
        if complete and view["replay"]["current_session"] is not None:
            raise ObservationError("completed_trial_has_unfinished_cursor")
        constraints = Counter(o["verification"]["constraint_diagnosis"] for o in orders)
        metrics = dict(
            metric_basis="saved_as_of_read_head_not_full_period_unless_completed",
            entry_condition_count=None,
            order_count=summary["order_count"],
            buy_count=sum(o["side"] == "BUY" for o in orders),
            sell_count=sum(o["side"] == "SELL" for o in orders),
            filled_count=counts["filled"],
            rejected_count=counts["rejected"],
            cancelled_count=counts["cancelled"] + counts["canceled"],
            waiting_count=counts["pending"] + counts["waiting"],
            terminal_order_count=terminal,
            fill_rate_percent=rate,
            reason_counts=dict(
                sorted(Counter(o["saved_reason"] or "none" for o in orders).items())
            ),
            constraint_counts=dict(sorted(constraints.items())),
            charged_commission_as_of=summary["charged_commission"],
            cash_as_of=view["account"]["cash"],
            equity_as_of=view["account"]["equity"],
            realized_profit_as_of=view["account"]["realized_profit"],
            holdings_as_of=view["positions"],
            final_cash=view["account"]["cash"] if complete else None,
            final_equity=view["account"]["equity"] if complete else None,
            end_holdings=view["positions"] if complete else None,
            completed_trades_as_of=extra.pop("completed_trades"),
            last_finalized_session=view["replay"]["last_finalized_session"],
            replay=view["replay"],
        )
        data = dict(
            trial_id=meta["trial_id"],
            run_id=meta["stream_id"],
            provenance=meta["provenance"],
            availability="completed" if complete else "incomplete",
            metadata=meta,
            audit_validation=view["validation"],
            metrics=metrics,
            limitations=LIMITATIONS,
            **extra,
        )
        # Include absent plan alternatives in the shared consistency check.
        files.verify()
        return cls(
            JsonObject.from_value(data),
            _EvidenceGroup((files, model.audit.files)),
            root,
        )


@dataclass(frozen=True)
class _EvidenceGroup:
    members: tuple

    def verify(self):
        for member in self.members:
            member.verify()

    @property
    def hashes(self):
        return sorted({h for m in self.members for h in m.hashes})


class TrialComparabilityAssessment:
    def assess(self, left, right):
        def flatten(value, prefix=""):
            result = {}
            for key, item in value.items():
                name = prefix + key
                if isinstance(item, dict) and item:
                    result.update(flatten(item, name + "."))
                else:
                    result[name] = item
            return result

        a, b = flatten(left["conditions"]), flatten(right["conditions"])
        fields = []
        for key in sorted(a.keys() | b.keys()):
            x, y = a.get(key), b.get(key)
            unknown = x is None or y is None or x == "unverified" or y == "unverified"
            status = "unverified" if unknown else "match" if x == y else "mismatch"
            # Different binaries do NOT establish different calculation rules.
            if key in ("implementation_hash", "settings_hash") and status == "mismatch":
                status = "unverified"
            fields.append(dict(condition=key, left=x, right=y, status=status))
        evidence_ok = all(
            t["input_validation"]["status"] == "saved_rows_checked"
            and t["audit_validation"] is not None
            and t["audit_validation"]["status"] == "saved_records_checked_not_replayed"
            for t in (left, right)
        )
        mismatch = any(f["status"] == "mismatch" for f in fields)
        unverified = not evidence_ok or any(f["status"] == "unverified" for f in fields)
        return dict(
            left_run=left["run_id"],
            right_run=right["run_id"],
            status="conditions_mismatch"
            if mismatch
            else "comparability_unverified"
            if unverified
            else "conditions_match",
            equivalence="unverified"
            if unverified
            else "saved_contracts_match_only"
            if not mismatch
            else "conditions_differ",
            fields=fields,
            evidence_sufficient_for_saved_comparison=evidence_ok,
        )


@dataclass(frozen=True)
class TrialComparisonBundle:
    payload: JsonObject
    inputs: tuple

    @property
    def files(self):
        return _EvidenceGroup(tuple(i.files for i in self.inputs))

    @property
    def input_root(self):
        return self.inputs[0].input_root


class TrialComparisonBuilder:
    def build(self, inputs):
        inputs = tuple(inputs)
        if not inputs:
            raise ObservationError("comparison_requires_explicit_inputs")
        runs = {}
        for item in inputs:
            item.files.verify()
            row = item.payload.to_dict()
            key = row["run_id"]
            if key in runs and runs[key] != row:
                raise ObservationError("same_run_id_conflicting_content")
            runs[key] = row
        rows = sorted(
            runs.values(),
            key=lambda r: (r["conditions"]["period"], r["trial_id"], r["run_id"]),
        )
        groups = {}
        for row in rows:
            key = digest(dict(plan=row["trial_id"], inputs=row["declared_input_hash"]))
            row["replica_group"] = key
            groups.setdefault(key, []).append(row["run_id"])
        data = dict(
            schema="trial-comparison-v1",
            trials=rows,
            assessments=[
                TrialComparabilityAssessment().assess(a, b)
                for a, b in combinations(rows, 2)
            ],
            replica_groups=[
                dict(group_id=k, runs=v, independent_sample_count=None)
                for k, v in sorted(groups.items())
            ],
            aggregation="none_replicas_not_independent_market_samples",
            provenance_notice="Each row retains provenance. Artificial or unacquired periods are not real market results.",
            fill_rate_definition=dict(
                numerator="filled",
                denominator="terminal_orders",
                terminal_statuses=sorted(TERMINAL),
                pending_excluded=True,
                zero_denominator=None,
            ),
            decimal_encoding="exact_decimal_strings_rate_128_digit_context_counts_authoritative",
            execution_invoked=False,
            formal_oos=False,
        )
        return TrialComparisonBundle(JsonObject.from_value(data), inputs)
