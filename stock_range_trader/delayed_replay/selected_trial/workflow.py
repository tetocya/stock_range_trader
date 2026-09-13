"""Explicit opt-in workflow. Approval, acquisition, and execution are distinct."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from delayed_replay.limited_trial.models import (
    LimitedProxyTrialPlan,
    ScopedResearchAuthorization,
    implementation_hash,
)
from delayed_replay.live_probe import ProbeError
from delayed_replay.serialization import JsonObject, digest, time_text
from delayed_replay.validation import ReplayContractError
from examples.validate_limited_proxy import compare

from .acquisition import AcquisitionStopped, Receipt, Transport, stage_a, stage_b
from .contract import (
    APPROVAL_REFERENCE,
    AcquisitionPlan,
    SelectedTrialPlan,
    new_acquisition,
)
from .pipeline import acquire, build_inputs, lot_review, save
from .service import SelectedPreflight, SelectedService


def write_once(path, value):
    encoded = JsonObject.from_value(value).encoded
    if path.exists():
        if path.read_text() != encoded:
            raise ReplayContractError("selected_output_exists_or_changed")
        return
    with path.open("x") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def prepare(root, review_hashes, approval_reference):
    """The caller must supply the actual owner approval reference, not a model flag."""
    if approval_reference != APPROVAL_REFERENCE:
        raise ReplayContractError("owner_approval_reference_required")
    root = Path(root)
    if (root / "acquisition_plan.json").exists() or (
        root / "acquisition.sqlite"
    ).exists():
        raise ReplayContractError("acquisition_already_prepared_do_not_reset")
    project = Path(__file__).parents[2]
    template = LimitedProxyTrialPlan(
        JsonObject.from_value(
            json.loads((project / "config/limited_proxy_trial_plan.json").read_text())
        )
    )
    for s, h in review_hashes.items():
        lot_review(root, h, s)
    plan = new_acquisition(
        template, project / "config/selected_proxy_strategy.yaml", review_hashes
    )
    write_once(root / "acquisition_plan.json", plan.payload.to_dict())
    write_once(
        root / "approval.json",
        dict(
            schema="selected-owner-approval-v1",
            acquisition_hash=plan.sha256,
            approval_reference=approval_reference,
            recorded_at=time_text(datetime.now(UTC)),
            scope="conditional_selected_plan_and_continuous_split_comparison_only",
            formal_oos=False,
        ),
    )
    Receipt(root / "acquisition.sqlite", plan).close()
    return plan


def load_plan(root):
    plan = AcquisitionPlan(
        JsonObject.from_value(json.loads((root / "acquisition_plan.json").read_text()))
    )
    auth = json.loads((root / "approval.json").read_text())
    if (
        auth["acquisition_hash"] != plan.sha256
        or auth["approval_reference"] != APPROVAL_REFERENCE
        or auth["scope"]
        != "conditional_selected_plan_and_continuous_split_comparison_only"
        or auth["formal_oos"] is not False
    ):
        raise ReplayContractError("selected_owner_approval_mismatch")
    if plan.payload.to_dict()["model_hash"] != implementation_hash():
        raise ReplayContractError("selected_model_changed")
    return plan, auth


def live_acquire(root, *, reviewed_free_window=False):
    root = Path(root)
    plan, _ = load_plan(root)
    if not reviewed_free_window:
        raise AcquisitionStopped("current_free_terms_review_required")
    now = datetime.now(UTC)
    if (
        date_value(plan, "run_end") > (now - timedelta(weeks=12)).date()
        or date_value(plan, "history_start") < (now - timedelta(days=730)).date()
    ):
        raise AcquisitionStopped("outside_conservative_free_window")
    key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not key:
        raise AcquisitionStopped("api_key_missing")
    if not (root / "acquisition.sqlite").is_file():
        raise AcquisitionStopped("existing_receipt_required_no_budget_reset")
    for symbol, h in plan.payload.to_dict()["lot_reviews"].items():
        lot_review(root, h, symbol)
    import jquantsapi

    receipt = Receipt(root / "acquisition.sqlite", plan)
    try:
        if any(e["kind"] == "stopped" for e in receipt.events()):
            raise AcquisitionStopped("acquisition_previously_stopped")
        result = acquire(
            Transport(receipt, client=jquantsapi.ClientV2(api_key=key)), root
        )
        write_once(
            root / "acquisition_result.json",
            dict(selection=result[0], stage_a=result[1], stage_b=result[2]),
        )
        write_once(root / "communication.json", receipt.statistics())
        return result
    except (ValueError, OSError, TypeError, KeyError, ProbeError) as exc:
        # Fixed reason codes only. Never serialize transport exception text/headers.
        reason = (
            str(exc)
            if isinstance(exc, AcquisitionStopped)
            else exc.reason
            if isinstance(exc, ProbeError)
            else "acquisition_contract_or_io_failed"
        )
        receipt.append("stopped", dict(reason=reason))
        save(
            root,
            dict(
                status="blocked",
                reason=reason,
                communication=receipt.statistics(),
                clearing="not_executed",
            ),
        )
        write_once(root / "communication.json", receipt.statistics())
        raise AcquisitionStopped(reason) from None
    finally:
        receipt.close()


def date_value(plan, key):
    from datetime import date

    return date.fromisoformat(plan.payload.to_dict()[key])


def construct(root):
    root = Path(root)
    acquisition, auth = load_plan(root)
    result = json.loads((root / "acquisition_result.json").read_text())
    # Read full receipt and require that every derived capture is an original durable response.
    if not (root / "acquisition.sqlite").is_file():
        raise AcquisitionStopped("existing_receipt_required_no_budget_reset")
    receipt = Receipt(root / "acquisition.sqlite", acquisition)
    try:
        events = receipt.events()
        captured = {
            digest(e["data"]["capture"]): e["data"]["capture"]
            for e in events
            if e["kind"] == "capture"
        }
        for c in list(result["stage_a"].values()) + list(result["stage_b"].values()):
            if digest(c) not in captured:
                raise ReplayContractError("uncaptured_input")
        if receipt.selection() != result["selection"]:
            raise ReplayContractError("selection_receipt_mismatch")
    finally:
        receipt.close()
    plan = build_inputs(
        acquisition, result["selection"], result["stage_a"], result["stage_b"], root
    )
    write_once(root / "trial_plan.json", plan.payload.to_dict())
    # This is a deterministic narrowing of the user's already recorded conditional permission.
    authorization = ScopedResearchAuthorization(
        JsonObject.from_value(
            dict(
                schema="limited-authorization-v1",
                plan_hash=plan.sha256,
                status="approved_for_limited_trial",
                approval_reference=auth["approval_reference"]
                + ":acquisition:"
                + acquisition.sha256,
                recorded_at=auth["recorded_at"],
            )
        )
    )
    write_once(root / "trial_authorization.json", authorization.payload.to_dict())
    result = SelectedPreflight.evaluate(plan, root / "inputs", root, authorization)
    write_once(root / "preflight.json", result.to_dict())
    return result


def execute(root):
    root = Path(root)
    load_plan(root)
    plan = SelectedTrialPlan(
        JsonObject.from_value(json.loads((root / "trial_plan.json").read_text()))
    )
    authorization = ScopedResearchAuthorization(
        JsonObject.from_value(
            json.loads((root / "trial_authorization.json").read_text())
        )
    )
    # Reload source lineage and causal finite features before creating either account.
    return compare(
        plan,
        authorization,
        root,
        root / "comparison",
        datetime.now(UTC),
        _service=SelectedService,
    )


def specification_summary(plan):
    return dict(
        acquisition_hash=plan.sha256,
        settings_hash=digest(
            dict(
                strategy=plan.payload.to_dict()["strategy"],
                terms=plan.payload.to_dict()["terms"],
                rules=plan.payload.to_dict()["rules"],
            )
        ),
        stage_a_queries=stage_a(),
        conditional_stage_b={s: stage_b(s) for s in plan.payload.to_dict()["symbols"]},
        initial_requests=11,
        maximum_http_attempts=20,
        maximum_elapsed_seconds=1200,
        restart_resets_budget=False,
        formal_oos=False,
    )
