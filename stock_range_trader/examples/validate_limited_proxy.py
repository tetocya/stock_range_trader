"""Offline preparation or explicitly plan-authorized saved-data comparison."""

import argparse
import json
from pathlib import Path

from delayed_replay.limited_trial.models import (
    LimitedProxyTrialPlan,
    ScopedResearchAuthorization,
)
from delayed_replay.limited_trial.preflight import LimitedTrialPreflight
from delayed_replay.limited_trial.proposal import saved_run_proposal
from delayed_replay.limited_trial.service import LimitedTrialService
from delayed_replay.serialization import JsonObject, parse_time
from delayed_replay.validation import ReplayContractError


def publish(path, files):
    if path.exists():
        raise ReplayContractError("limited_output_exists")
    path.mkdir(parents=True, exist_ok=False)
    for name, payload in files.items():
        with (path / name).open("x") as f:
            f.write(payload.encoded)


def logical(state):
    return JsonObject.from_value(
        {
            k: v
            for k, v in state.items()
            if k
            not in (
                "identity",
                "inputs",
                "input_head",
                "versions",
                "operations",
                "phase_observations",
                "accepted_packets",
            )
        }
    )


def compare(plan, authorization, saved_run, output, wall):
    packet_root, evidence_root = saved_run / "inputs", saved_run
    LimitedTrialService._prepare(plan, authorization, packet_root, evidence_root)
    if output.exists():
        raise ReplayContractError("limited_output_exists")
    output.mkdir(parents=True, exist_ok=False)
    continuous = LimitedTrialService.create(
        output / "continuous.sqlite",
        plan,
        authorization,
        packet_root,
        evidence_root,
        "continuous",
    )
    try:
        expected = continuous.run(wall)
        report = continuous.report()
    finally:
        continuous.store.close()
    split = LimitedTrialService.create(
        output / "split.sqlite",
        plan,
        authorization,
        packet_root,
        evidence_root,
        "split_resume",
    )
    try:
        waiting = split.run(wall)
        if waiting["status"] != "waiting_for_input":
            raise ReplayContractError("limited_split_did_not_wait")
        prefix = split.store.read().events
    finally:
        split.store.close()
    split = LimitedTrialService.resume(
        output / "split.sqlite",
        plan,
        authorization,
        packet_root,
        evidence_root,
        "split_resume",
    )
    try:
        parent = split.state["input_head"]
        sha = plan.payload.to_dict()["packets"][1]
        split.accept(sha, "part-2", parent, wall)
        split.accept(sha, "part-2", parent, wall)
        actual = split.run(wall)
        prefix_equal = split.store.read().events[: len(prefix)] == prefix
        match = logical(actual) == logical(expected)
        if not prefix_equal or not match:
            raise ReplayContractError("limited_comparison_mismatch")
        comparison = JsonObject.from_value(
            dict(
                schema="limited-comparison-v1",
                plan_hash=plan.sha256,
                continuous_logical_hash=logical(expected).sha256,
                split_logical_hash=logical(actual).sha256,
                prefix_unchanged=prefix_equal,
                logical_state_equal=match,
                input_delivery="saved_data_two_parts_not_live_publication",
                report=report.to_dict(),
            )
        )
        with (output / "comparison.json").open("x") as handle:
            handle.write(comparison.encoded)
        return comparison
    finally:
        split.store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("propose", "preflight", "compare"))
    parser.add_argument("--saved-run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--replayed-at")
    args = parser.parse_args(argv)
    try:
        plan = (
            saved_run_proposal(args.saved_run)
            if args.action == "propose"
            else LimitedProxyTrialPlan(
                JsonObject.from_value(json.loads(args.plan.read_text()))
            )
        )
        auth = (
            None
            if args.authorization is None
            else ScopedResearchAuthorization(
                JsonObject.from_value(json.loads(args.authorization.read_text()))
            )
        )
        if args.action == "compare":
            if args.replayed_at is None:
                raise ReplayContractError("limited_explicit_replay_clock_required")
            compare(
                plan, auth, args.saved_run, args.output, parse_time(args.replayed_at)
            )
        else:
            result = LimitedTrialPreflight.evaluate(
                plan, args.saved_run / "inputs", args.saved_run, auth
            )
            publish(args.output, {"plan.json": plan.payload, "preflight.json": result})
        print(
            json.dumps(
                dict(
                    status="preparation_completed"
                    if args.action != "compare"
                    else "comparison_completed",
                    plan_hash=plan.sha256,
                )
            )
        )
        return 0
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        print(
            json.dumps(dict(status="failed", reason="limited_contract_or_input_error"))
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
