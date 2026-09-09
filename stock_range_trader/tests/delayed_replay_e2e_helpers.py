"""Offline integration harness. All settings/evidence are explicitly synthetic."""

import hashlib
import json
import socket
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from delayed_replay_stage4_helpers import WALL, fixture_plan

from data.price_policy import provider_price_basis
from delayed_replay.account_models import SharedAccountState
from delayed_replay.checkpoint_models import CheckpointSchedule, FinalizationPolicy
from delayed_replay.checkpoint_report import (
    ARTIFACT_SCHEMA,
    ARTIFACTS_FILENAME,
    CHECKPOINTS_FILENAME,
    REGISTRATION_FILENAME,
    RESULT_FILENAME,
    write_checkpoint_results,
    write_registration_candidate,
)
from delayed_replay.checkpoints import LedgerCheckpointSource
from delayed_replay.protocol_judge import ProtocolJudge
from delayed_replay.registration_candidate import (
    RegistrationInputs,
    SourceIdentity,
    build_registration_candidate,
)
from delayed_replay.replay_engine import ReplayEngine
from delayed_replay.replay_policy import fingerprint
from delayed_replay.replay_state import ReplayReducer
from delayed_replay.serialization import JsonObject


def forbidden(*args, **kwargs):
    raise AssertionError("offline_e2e_forbidden_side_effect")


@contextmanager
def offline():
    """Guard HTTP/provider entrances and IP sockets, retaining local AF_UNIX IPC."""
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    sendto = socket.socket.sendto

    def guarded(original):
        def call(sock, *args, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                forbidden()
            return original(sock, *args, **kwargs)

        return call

    with ExitStack() as stack:
        for name in (
            "requests.sessions.Session.request",
            "urllib.request.urlopen",
            "urllib3.connectionpool.HTTPConnectionPool.urlopen",
            "data.providers.jquants_v2.JQuantsV2Provider.get_daily_bars",
            "data.providers.jquants_v2.JQuantsV2Provider.get_universe",
            "data.providers.jquants_v2.JQuantsV2Provider.get_trading_calendar",
            "data.providers.yfinance.YFinanceProvider.get_daily_bars",
            "socket.create_connection",
            "socket.getaddrinfo",
        ):
            stack.enter_context(patch(name, forbidden))
        for name, original in (
            ("connect", connect),
            ("connect_ex", connect_ex),
            ("sendto", sendto),
        ):
            stack.enter_context(patch.object(socket.socket, name, guarded(original)))
        # yfinance uses curl_cffi (C sockets bypass Python socket methods).
        stack.enter_context(patch("curl_cffi.requests.Session.request", forbidden))
        yield


@pytest.fixture
def network_guard():
    with offline():
        yield


def policies():
    policy = FinalizationPolicy(
        WALL - timedelta(days=1), None, WALL + timedelta(days=1), "explicit_synthetic"
    )
    return {1: policy, 3: policy}


def candidate_for(plan):
    """No account/result argument; unknown Git/approvals stay unverified."""
    return build_registration_candidate(
        RegistrationInputs(
            protocol_hash=plan.protocol_hash,
            source=SourceIdentity("git_unavailable", None, None),
            account_policy=plan.account_policy,
            replay_policy=plan.policy,
            config_hash=fingerprint(plan.signals.base_config),
            catalog_hash=fingerprint(plan.signals.catalog),
            selection_policy_hash=fingerprint(plan.monthly.selector.policy),
            price_contract_hash=fingerprint(provider_price_basis("jquants")),
            universe=plan.universe,
            universe_reference_date=plan.policy.run_start,
            calendar_hash=plan.calendar.sha256,
            planned_start=plan.policy.run_start,
            one_month_finalization=policies()[1],
            three_month_finalization=policies()[3],
            history_snapshot_hashes=(),  # No approved historical source evidence.
            verification=(),
            unresolved_operational_fields=(
                "fees",
                "max_positions",
                "lookback_months",
                "max_position_pct",
            ),
        ),
        generated_at=WALL,
    )


def checkpoints(plan, records, previous=None):
    return LedgerCheckpointSource(records, records.head).collect(
        CheckpointSchedule(plan.policy.run_start, plan.calendar),
        policies(),
        now=WALL,
        previous=previous,
    )


@dataclass
class Run:
    plan: object
    path: Path
    candidate: object
    records: object
    pair: tuple
    result: object
    bundle: Path

    @property
    def state(self):
        return self.records.current_state.to_dict()


def export(engine, root, candidate, previous=None):
    before = engine.store.read()
    # Pure reducer validation is allowed; orchestration and new commits are not.
    with ExitStack() as stack:
        for cls, method in (
            (ReplayEngine, "run"),
            (ReplayEngine, "advance"),
            (type(engine.plan.monthly.evaluator), "evaluate_validation"),
            (type(engine.plan.monthly.selector), "select"),
            (type(engine.plan.signals), "decide"),
            (type(engine.store), "commit_event"),
        ):
            stack.enter_context(patch.object(cls, method, forbidden))
        pair = checkpoints(engine.plan, before, previous)
        result = ProtocolJudge().evaluate(*pair, now=WALL)
        bundle = write_checkpoint_results(*pair, result, root / "result")
    assert engine.store.read() == before
    return Run(
        engine.plan, root / "replay.sqlite", candidate, before, pair, result, bundle
    )


def run(root, plan=None):
    root.mkdir(parents=True)
    plan = fixture_plan() if plan is None else plan
    with offline():
        candidate = candidate_for(plan)
        write_registration_candidate(candidate, root / "candidate")
        engine = ReplayEngine.create(root / "replay.sqlite", plan)
        try:
            engine.run(WALL)
            return export(engine, root, candidate)
        finally:
            engine.store.close()


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    return run(tmp_path_factory.mktemp("stage6") / "baseline")


def timeline(records):
    state = records.initial_state.to_dict()
    for event in records.events:
        state = ReplayReducer()(state, event.command)
        yield event, state


def artifact_bytes(root):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.suffix in (".json", ".csv")
    }


def verify_artifacts(run):
    candidate = json.loads(
        (run.bundle.parent / "candidate" / REGISTRATION_FILENAME).read_text()
    )
    claimed = candidate.pop("payload_sha256")
    candidate_id = candidate.pop("candidate_id")
    candidate.pop("generated_at")
    normalized = json.dumps(
        candidate,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert hashlib.sha256(normalized).hexdigest() == claimed
    assert candidate_id == "registration-candidate-" + claimed
    metadata = json.loads((run.bundle / ARTIFACTS_FILENAME).read_text())
    assert metadata["schema"] == ARTIFACT_SCHEMA
    assert set(metadata["files"]) == {CHECKPOINTS_FILENAME, RESULT_FILENAME}
    for name, expected in metadata["files"].items():
        assert hashlib.sha256((run.bundle / name).read_bytes()).hexdigest() == expected
    for body in artifact_bytes(run.bundle.parent).values():
        assert str(run.bundle.parent).encode() not in body
        # The lane label "raw_ohlcv" is public policy, not raw observation data.
        assert b"/Users/" not in body and b'"raw_ohlcv":' not in body
        assert b"JQUANTS_API_KEY" not in body


def reconcile(plan, records):
    """Independent Decimal arithmetic over ledger fills; no production cost helper."""
    D = Decimal
    opens = {
        (e.instrument_id, e.session): D(e.open_price)
        for s in plan.market.open_snapshots
        for e in s.evidence
    }
    seen_reservation = False
    for _, state in timeline(records):
        account = state["account"]
        cash, quantities = D("200000"), {}
        orders = account["orders"]
        for order in orders.values():
            if order["status"] != "filled":
                continue
            req, fill, shares = order["request"], order["fill"], order["shares"]
            symbol, buy = req["instrument_id"], req["side"] == "BUY"
            assert shares > 0 and shares % 100 == 0
            price = (
                opens[symbol, fill["session"]]
                * (1 + D(plan.account_policy.slippage_pct) * (1 if buy else -1))
            ).quantize(D("0.01"), rounding=ROUND_CEILING if buy else ROUND_FLOOR)
            gross = (price * shares).quantize(D("0.01"), rounding=ROUND_HALF_EVEN)
            fee = (gross * D(plan.account_policy.commission_rate)).quantize(
                D("0.01"), rounding=ROUND_CEILING
            )
            net = gross + fee if buy else gross - fee
            assert (
                D(fill["price"]),
                D(fill["gross"]),
                D(fill["commission"]),
                D(fill["net_amount"]),
            ) == (price, gross, fee, net)
            cash += -net if buy else net
            quantities[symbol] = quantities.get(symbol, 0) + (
                shares if buy else -shares
            )
        assert cash == D(account["cash"])
        assert {s: q for s, q in quantities.items() if q} == {
            s: p["shares"] for s, p in account["positions"].items()
        }
        reserved = sum(
            (
                D(o["reservation"]["cash"])
                for o in orders.values()
                if not o["reservation"]["released"]
            ),
            D(0),
        )
        holds = sum((D(h["amount"]) for h in account["proceeds_holds"].values()), D(0))
        model = SharedAccountState(JsonObject.from_value(account))
        assert model.reserved_cash == reserved + holds
        assert model.available_cash == cash - reserved - holds >= 0
        seen_reservation |= reserved > 0
        if account["valuation"]["complete"]:
            equity = cash + sum(
                (
                    p["shares"] * D(account["valuation"]["marks"][s]["price"])
                    for s, p in account["positions"].items()
                ),
                D(0),
            )
            assert equity == D(account["valuation"]["display_equity"])
        exits = set()
        for episode_id, episode in account["episodes"].items():
            pos = episode["position"]
            assert (
                episode_id
                == pos["episode_id"]
                == pos["position_id"]
                == pos["entry_order_id"]
            )
            entry, exit_order = (
                orders[pos["entry_order_id"]],
                orders[episode["exit_order_id"]],
            )
            assert episode["exit_order_id"] not in exits
            exits.add(episode["exit_order_id"])
            assert entry["shares"] == exit_order["shares"] == pos["shares"]
            assert pos["candidate_id"] == exit_order["request"]["candidate_id"]
            assert pos["exit_config_hash"] == exit_order["request"]["exit_config_hash"]
            assert D(pos["entry_commission"]) == D(entry["fill"]["commission"])
            assert D(episode["net_profit"]) == D(exit_order["fill"]["net_amount"]) - D(
                entry["fill"]["net_amount"]
            )
        assert len(exits) == sum(
            o["status"] == "filled" and o["request"]["side"] == "SELL"
            for o in orders.values()
        )
    assert seen_reservation


if __name__ == "__main__":
    # Only the test worker uses this entry point, never a formal execution CLI.
    import sys

    root, mode = Path(sys.argv[1]), sys.argv[2]
    with offline():
        # Guard self-check also runs in each independently launched interpreter.
        with (
            socket.socket() as sock,
            pytest.raises(AssertionError, match="offline_e2e"),
        ):
            sock.connect(("192.0.2.1", 443))
        plan = fixture_plan()
        if mode == "cut":
            root.mkdir()
            candidate = candidate_for(plan)
            write_registration_candidate(candidate, root / "candidate")
            engine = ReplayEngine.create(root / "replay.sqlite", plan)
            while engine.state.to_dict()["cursor"] != {"index": 14, "phase": "open"}:
                assert engine.advance(WALL) == "running"
            # Deliberate abrupt process exit: SQLite committed, no graceful close.
            import os

            os._exit(23)
        elif mode == "resume":
            with (
                patch.object(
                    type(plan.monthly.evaluator), "evaluate_validation", forbidden
                ),
                patch.object(type(plan.monthly.selector), "select", forbidden),
            ):
                engine = ReplayEngine.resume(root / "replay.sqlite", plan)
                try:
                    engine.run(WALL)
                    export(engine, root, candidate_for(plan))
                finally:
                    engine.store.close()
        else:
            raise ValueError("unknown_worker_mode")
