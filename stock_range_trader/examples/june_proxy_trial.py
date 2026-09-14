"""June input preparation; no clearing command and no implicit market access."""

import argparse
import json
from pathlib import Path

from delayed_replay import june_trial as june


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("prepare", "inspect", "acquire", "resume", "build-inputs")
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--may-root", type=Path, required=True)
    parser.add_argument("--lot-review-reference")
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--reviewed-free-window", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            plan = june.prepare(args.root, args.may_root, args.lot_review_reference)
            result = dict(status="prepared_not_authorized", plan_hash=plan.sha256)
        elif args.action == "inspect":
            result = june.inspect(args.root, args.may_root)
        elif args.action == "build-inputs":
            result = june.build(args.root, args.may_root)
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
