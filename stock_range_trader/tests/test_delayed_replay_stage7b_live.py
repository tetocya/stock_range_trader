"""One explicitly opted-in acquisition probe, not strategy acceptance."""

import json
import os
import uuid
from datetime import date
from pathlib import Path

import pytest

from delayed_replay.live_probe import ProbeConfig, run_probe


@pytest.mark.live_jquants
def test_stage7b_bounded_live_acquisition():
    if (
        os.environ.get("RUN_LIVE_JQUANTS_TESTS") != "1"
        or os.environ.get("RUN_LIVE_STAGE7B_TESTS") != "1"
    ):
        pytest.skip("Stage7B requires both explicit Live opt-ins")
    if not os.environ.get("JQUANTS_API_KEY", "").strip():
        pytest.skip("Stage7B Live not performed: API key missing")
    required = ("STAGE7B_START", "STAGE7B_END", "STAGE7B_MASTER_DATE")
    if (
        any(not os.environ.get(k) for k in required)
        or os.environ.get("STAGE7B_REVIEWED") != "1"
    ):
        pytest.fail("Stage7B requires explicit dates and current terms/window review")
    config = ProbeConfig(*(date.fromisoformat(os.environ[k]) for k in required))
    root = (
        Path(__file__).resolve().parents[1]
        / ".delayed_replay"
        / "stage7b"
        / uuid.uuid4().hex
    )
    report = run_probe(config, root, live=True, reviewed=True)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "report.json").open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
    # Network failure is a failed test, never skip/success. No raw assertion data.
    assert report["layers"]["communication"]["status"] == "verified", (
        "Live communication unverified"
    )
    assert report["layers"]["data"]["status"] == "verified", (
        "Live data contract unverified"
    )
    assert report["layers"]["persistence"]["status"] == "verified", (
        "Live persistence unverified"
    )
