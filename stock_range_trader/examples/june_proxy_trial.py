"""June preparation and separately authorized saved-data research commands."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from delayed_replay import june_clearing as clearing
from delayed_replay import june_trial as june
from delayed_replay.serialization import JsonObject, parse_time


def clearing_command(args):
    if not args.execute_saved_data or args.clearing_authorization is None:
        raise ValueError("explicit_separate_clearing_authorization_required")
    plan = clearing.JuneClearingPlan(
        JsonObject.from_value(june.load_json(args.root / "clearing_plan.json"))
    )
    if plan.payload.to_dict()["provenance"] != "saved_jquants":
        raise ValueError("real_entry_rejects_artificial_provenance")
    authorization = clearing.JuneClearingAuthorization(
        JsonObject.from_value(june.load_json(args.clearing_authorization))
    )
    wall = parse_time(args.replayed_at) if args.replayed_at else datetime.now(UTC)
    if args.action == "compare-clearing":
        if args.output is None:
            raise ValueError("separate_output_required")
        return clearing.compare(
            args.root, args.may_root, plan, authorization, args.output, wall
        )
    if args.account is None:
        raise ValueError("separate_account_path_required")
    method = (
        clearing.JuneClearingService.create
        if args.action == "start-clearing"
        else clearing.JuneClearingService.resume
    )
    service = method(
        args.account, plan, authorization, args.root, args.may_root, args.style
    )
    try:
        if args.action == "accept-inputs":
            state = service.state
            if state["status"] != "waiting_for_input":
                raise ValueError("waiting_account_required")
            service.accept(
                plan.payload.to_dict()["packets"][1],
                "june-part-2",
                state["input_head"],
                wall,
            )
            return dict(
                status="additional_input_accepted_not_advanced",
                input_head=service.state["input_head"],
            )
        service.run(wall)
        return service.report().to_dict()
    finally:
        service.store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "prepare",
            "inspect",
            "acquire",
            "resume",
            "build-inputs",
            "prepare-clearing",
            "start-clearing",
            "accept-inputs",
            "resume-clearing",
            "compare-clearing",
        ),
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--may-root", type=Path, required=True)
    parser.add_argument("--lot-review-reference")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--reviewed-free-window", action="store_true")
    parser.add_argument("--clearing-authorization", type=Path)
    parser.add_argument("--execute-saved-data", action="store_true")
    parser.add_argument("--account", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--style", choices=("continuous", "split_resume"), default="split_resume"
    )
    parser.add_argument("--replayed-at")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            plan = june.prepare(args.root, args.may_root, args.lot_review_reference)
            result = dict(status="prepared_not_authorized", plan_hash=plan.sha256)
        elif args.action == "inspect":
            result = june.inspect(args.root, args.may_root)
        elif args.action == "build-inputs":
            result = june.build(args.root, args.may_root)
        elif args.action == "prepare-clearing":
            plan = clearing.prepare_clearing(args.root, args.may_root)
            result = dict(
                status="prepared_not_authorized", clearing_plan_hash=plan.sha256
            )
        elif args.action in (
            "start-clearing",
            "accept-inputs",
            "resume-clearing",
            "compare-clearing",
        ):
            result = clearing_command(args)
        else:
            if not args.live or args.authorization is None:
                raise ValueError("explicit_live_and_separate_authorization_required")
            result = june.live_acquire(
                args.root,
                args.may_root,
                args.authorization,
                resume=args.action == "resume",
                reviewed_free_window=args.reviewed_free_window,
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError, AttributeError):
        print(
            json.dumps(
                dict(
                    status="blocked",
                    reason="june_contract_evidence_permission_or_budget",
                    clearing="not_executed",
                )
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
