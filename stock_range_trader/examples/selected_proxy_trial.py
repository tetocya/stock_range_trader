"""Explicit preparation / bounded live acquisition / offline research execution."""

import argparse
import json
from pathlib import Path

from delayed_replay.selected_trial import workflow
from delayed_replay.validation import ReplayContractError


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("prepare", "acquire", "construct", "execute"))
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--reviews", type=Path)
    p.add_argument("--approval-reference")
    p.add_argument("--live", action="store_true")
    p.add_argument("--reviewed-free-window", action="store_true")
    a = p.parse_args(argv)
    try:
        if a.action == "prepare":
            result = workflow.prepare(
                a.root, json.loads(a.reviews.read_text()), a.approval_reference
            )
            output = workflow.specification_summary(result)
        elif a.action == "acquire":
            if not a.live:
                raise ReplayContractError("explicit_live_opt_in_required")
            result = workflow.live_acquire(
                a.root, reviewed_free_window=a.reviewed_free_window
            )
            output = dict(
                status="acquisition_completed", representative=result[0]["symbol"]
            )
        elif a.action == "construct":
            output = workflow.construct(a.root).to_dict()
        else:
            output = workflow.execute(a.root).to_dict()
        print(json.dumps(output, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, ReplayContractError)
            else "selected_contract_or_io_failed"
        )
        print(json.dumps(dict(status="stopped", reason=reason, formal_oos=False)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
