"""Read-only diagnostics: never select candidates, simulate orders, or approve."""

from datetime import date

from data.price_policy import provider_price_basis
from delayed_replay.account_policy import D, M
from delayed_replay.reference_evidence import LotEvidence, ReferenceReview
from delayed_replay.serialization import JsonObject
from delayed_replay.sizing import size_buy
from delayed_replay.validation import ReplayContractError

from .inputs import LimitedEvidenceBlocked, SavedProxyInputs
from .models import SCOPE, implementation_hash, warmup_requirements


class LimitedTrialPreflight:
    @staticmethod
    def evaluate(plan, packet_root, evidence_root, authorization=None):
        p = plan.payload.to_dict()
        result = dict(
            schema="limited-preflight-v1",
            scope=SCOPE,
            plan_hash=plan.sha256,
            model_hash=p["model_hash"],
            model_approval="unapproved",
            clearing="not_executed",
            fill_count=None,
            checks={},
            reasons=[],
            unverified=[
                "actual_fills",
                "order_waiting",
                "real_account_arithmetic",
                "real_restart",
                "external_price_comparison",
                "actual_trade_time",
                "halt_coverage",
            ],
            inventory=None,
            history=None,
            affordability=None,
        )
        checks = result["checks"]

        def check(name, status, reason):
            checks[name] = dict(status=status, reason=reason)
            if status in ("failed", "unsupported", "blocked"):
                result["reasons"].append(reason)

        check(
            "implementation",
            "verified" if p["model_hash"] == implementation_hash() else "unsupported",
            "implementation_hash_match"
            if p["model_hash"] == implementation_hash()
            else "implementation_changed",
        )
        if authorization is None:
            check("authorization", "blocked", "approval_pending")
        else:
            try:
                authorization.require(plan)
                check(
                    "authorization",
                    "verified",
                    authorization.payload.to_dict()["status"],
                )
            except (ValueError, TypeError):
                check("authorization", "blocked", "approval_scope_mismatch")
        policy = signals = bundle = None
        try:
            policy = plan.policy
            policy.require()
            signals = plan.signals()
            check(
                "configuration",
                "verified",
                "explicit_configuration_not_operational_approval",
            )
        except (ValueError, TypeError, KeyError):
            unresolved = any(
                p[k] is None for k in ("terms", "rules", "strategy", "candidate_id")
            ) or any(
                v is None for key in ("terms", "rules") for v in (p[key] or {}).values()
            )
            check(
                "configuration",
                "blocked" if unresolved else "unsupported",
                "unresolved_configuration"
                if unresolved
                else "unsupported_configuration",
            )
        try:
            bundle = SavedProxyInputs.load(plan, packet_root, evidence_root)
            result["inventory"] = bundle.inventory.to_dict()
            check("saved_inputs", "verified", "hash_scope_calendar_and_capture_lineage")
        except LimitedEvidenceBlocked:
            check("saved_inputs", "blocked", "calendar_evidence_incomplete")
        except (OSError, ValueError, TypeError, KeyError):
            check("saved_inputs", "failed", "saved_input_integrity_or_contract_failed")
        try:
            r = p["references"]["lot"]
            if r is None:
                raise ReplayContractError("lot_missing")
            lot = LotEvidence(
                r["instrument"],
                date.fromisoformat(r["start"]),
                date.fromisoformat(r["end"]),
                r["lot_size"],
                ReferenceReview(r["source"], r["subject_hash"], r["review_hash"]),
            )
            if lot.subject_hash != r["subject_hash"]:
                check("lot", "failed", "lot_subject_hash_mismatch")
            elif lot.lot_size != 100:
                check("lot", "unsupported", "lot_size_unsupported")
            else:
                for day in (SCOPE["start"], "2026-05-31"):
                    lot.require(SCOPE["symbol"], date.fromisoformat(day))
                check(
                    "lot",
                    "verified",
                    "structural_review_and_effective_period_only_not_authenticity_proof",
                )
        except (ValueError, TypeError, KeyError):
            check("lot", "blocked", "lot_period_or_review_missing")
        price = p["references"]["price"]
        if price == dict(
            basis=provider_price_basis("jquants"),
            volume_unit="execution_shares",
            review_scope="provider_contract_and_saved_capture_only",
        ):
            check(
                "price_basis",
                "verified",
                "provider_contract_only_external_comparison_unverified",
            )
        else:
            check("price_basis", "unsupported", "price_basis_or_volume_unit_mismatch")
        halt = p["references"]["halt"]
        if (
            not isinstance(halt, dict)
            or set(halt) != {"coverage", "observations"}
            or halt["coverage"] != "unknown"
        ):
            check("halt", "unsupported", "halt_policy_unsupported")
        elif any(
            v not in ("unknown", "full", "temporary", "delayed_first_trade")
            for v in halt["observations"].values()
        ):
            check("halt", "unsupported", "halt_evidence_invalid")
        else:
            check("halt", "verified", "known_evidence_checked_coverage_unknown")
        if bundle:
            rows = bundle.rows.to_dict()
            actions = any(
                v["row"]["adjustment_factor"] != "1"
                or v["stock_split"] != "0"
                or v["ex_right"] is not None
                for v in rows.values()
            )
            check(
                "corporate_action",
                "unsupported" if actions else "verified",
                "corporate_action_unsupported"
                if actions
                else "saved_rows_no_reported_action_not_external_proof",
            )
            if isinstance(halt, dict) and isinstance(halt.get("observations"), dict):
                for day, state in halt["observations"].items():
                    row = rows.get(SCOPE["symbol"] + "|" + day, {}).get("row")
                    if day not in bundle.sessions or (
                        state == "full"
                        and row
                        and (D(row["open"]) > 0 or D(row["volume"]) > 0)
                    ):
                        check("halt", "failed", "halt_evidence_contradiction")
            if signals:
                requirements = warmup_requirements(signals.config(p["candidate_id"]))
                before = sum(
                    v["row"]["session"] < SCOPE["start"] for v in rows.values()
                )
                needed = max(requirements.values()) - 1
                result["history"] = dict(
                    requirements=requirements,
                    required_before_first_close=needed,
                    available_before_start=before,
                    available_in_period=len(bundle.sessions),
                    monthly_reference=p["monthly_reference"],
                    candidate_supply=p["candidate_supply"],
                    history_packets=p["history_packets"],
                    implicit_history_addition=False,
                )
                check(
                    "history",
                    "verified" if before >= needed else "blocked",
                    "history_count_sufficient_no_signal_run"
                    if before >= needed
                    else "insufficient_history",
                )
            if policy:
                try:
                    a = policy.require()
                    first = rows[SCOPE["symbol"] + "|" + bundle.sessions[0]]["row"]
                    q = size_buy(
                        first["close"], a.initial_capital, a.initial_capital, a
                    )
                    result["affordability"] = dict(
                        reference_date=first["session"],
                        reference_field="close",
                        purpose="static_diagnostic_not_pre_session_order",
                        budget=q.frozen_budget,
                        affordable_shares=q.shares,
                        status="insufficient_lot_budget"
                        if q.shares == 0
                        else "lot_budget_possible",
                        ratio=a.max_position_pct,
                        initial_capital=a.initial_capital,
                        lot_size=a.lot_size,
                        one_lot_reference_cost=M(
                            sum(
                                a.cost(
                                    a.price(
                                        D(first["close"]) * (1 + D(a.slippage_pct)),
                                        "BUY",
                                    ),
                                    100,
                                )
                            )
                        ),
                    )
                except (ValueError, TypeError):
                    pass
        result["ready"] = not result["reasons"]
        result["status"] = next(
            (
                state
                for state in ("failed", "unsupported", "blocked")
                if any(c["status"] == state for c in checks.values())
            ),
            "ready",
        )
        return JsonObject.from_value(result)
