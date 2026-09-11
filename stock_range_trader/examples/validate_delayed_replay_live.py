"""Bounded 7B preflight / opt-in acquisition. No account or ProtocolJudge."""

import argparse
import json
import uuid
from datetime import date
from pathlib import Path

from delayed_replay.live_probe import ProbeConfig, run_probe


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--master-date", type=date.fromisoformat, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--reviewed-free-window-and-terms", action="store_true")
    args = parser.parse_args(argv)
    config = ProbeConfig(args.start, args.end, args.master_date)
    print(json.dumps(config.preflight(), ensure_ascii=False, indent=2))
    root = (
        Path(__file__).resolve().parents[1]
        / ".delayed_replay"
        / "stage7b"
        / uuid.uuid4().hex
    )
    result = run_probe(
        config, root, live=args.live, reviewed=args.reviewed_free_window_and_terms
    )
    root.mkdir(parents=True, exist_ok=True)
    with (root / "report.json").open("x", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.live:
        return 0
    return (
        0 if result["end_reason"] == "data_probe_finished_execution_unsupported" else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
