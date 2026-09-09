"""Frozen protocol precedence. No MDD gates, stop commands or external calls."""

from datetime import datetime, time

from .checkpoint_models import (
    JUDGE_VERSION,
    REGISTRATION_STATUS,
    CheckpointEvidence,
    ProtocolResult,
    exact,
)
from .replay_calendar import JST
from .serialization import JsonObject, time_text
from .validation import ReplayContractError, timestamp


class ProtocolJudge:
    def evaluate(self, one: CheckpointEvidence, three: CheckpointEvidence, *, now):
        timestamp(now, "now")
        if (
            not isinstance(one, CheckpointEvidence)
            or not isinstance(three, CheckpointEvidence)
            or one.months != 1
            or three.months != 3
            or one.start != three.start
            or one.initial_equity != three.initial_equity
            or one.evidence_kind != three.evidence_kind
        ):
            raise ReplayContractError("incompatible_checkpoint_pair")
        for field in ("account_stream_id", "genesis_hash"):
            if one.references.to_dict().get(field) != three.references.to_dict().get(
                field
            ):
                raise ReplayContractError("checkpoint_account_or_head_mismatch")
        first_head = one.references.to_dict()["expected_head"]
        final_refs = three.references.to_dict()
        if first_head != final_refs[
            "expected_head"
        ] and first_head not in final_refs.get("verified_head_ancestry", []):
            raise ReplayContractError("checkpoint_head_not_verified_ancestor")
        for e in (one.valuation, one.samples, three.valuation, three.samples):
            if (e.observed_at is not None and e.observed_at > now) or (
                e.finalized_at is not None and e.finalized_at > now
            ):
                raise ReplayContractError("future_finalization_evidence")
        gate = "pending"
        if one.valuation.status == "available":
            gate = (
                "failed"
                if exact(one.equity) < exact(one.initial_equity) * exact("0.95")
                else "passed"
            )
        elif one.valuation.status == "unavailable_external":
            gate = "unavailable"
        if any(
            e.status == "invalid"
            for e in (one.valuation, one.samples, three.valuation, three.samples)
        ):
            label, reason = "INVALID", "invalid_evidence"
        elif now < datetime.combine(three.boundary, time.min, JST):
            label, reason = "PENDING", "three_month_period_not_complete"
        elif one.valuation.status == "pending":
            label, reason = "PENDING", "one_month_gate_not_finalized"
        elif gate == "unavailable":
            label, reason = "INCONCLUSIVE", "one_month_return_unavailable"
        elif gate == "failed":
            label, reason = "FAIL", "early_downside_gate_breached"
        elif three.samples.status == "pending":
            label, reason = "PENDING", "sample_evidence_pending"
        elif three.samples.status == "unavailable_external":
            label, reason = "INCONCLUSIVE", "sample_evidence_unavailable"
        elif three.unique_symbols < 20 or three.completed_trades < 100:
            label, reason = "INCONCLUSIVE", "insufficient_sample"
        elif three.valuation.status == "pending":
            label, reason = "PENDING", "three_month_valuation_pending"
        elif three.valuation.status == "unavailable_external":
            label, reason = "INCONCLUSIVE", "three_month_return_unavailable"
        elif exact(three.equity) > exact(three.initial_equity):
            label, reason = "PASS", "positive_primary_return"
        else:
            label, reason = "FAIL", "non_positive_primary_return"
        value = JsonObject.from_value(
            dict(
                schema="delayed-protocol-result-1",
                policy_version=JUDGE_VERSION,
                lifecycle="pending" if label == "PENDING" else "finalized",
                validity="invalid" if label == "INVALID" else "valid",
                outcome=label if label in ("PASS", "FAIL", "INCONCLUSIVE") else "N/A",
                label=label,
                one_month_gate=gate,
                reason_codes=[reason],
                evidence_kind=one.evidence_kind,
                registration_status=REGISTRATION_STATUS,
                formal_registration_performed=False,
                execution_mode="delayed_oos_replay",
                assessed_at=time_text(now),
                evidence_hashes=[one.sha256, three.sha256],
                accounting_constraints=[
                    "dividends_not_added",
                    "before_tax",
                    "no_hypothetical_exit_costs",
                ],
                evidence_references=three.references.to_dict(),
            )
        )
        return ProtocolResult(value, value.sha256)
