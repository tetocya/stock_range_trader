"""Offline input file/DB acceptance, isolation and same-account continuation."""

from dataclasses import replace
from datetime import date, timedelta

import pytest
from delayed_replay_e2e_helpers import network_guard as network_guard
from delayed_replay_stage4_helpers import WALL, fixture_plan, reprice

from delayed_replay.input_artifacts import InputArtifactStore, InputPacket
from delayed_replay.input_versions import ExtensibleReplayEngine, InputExtensionEvent
from delayed_replay.market_view import MarketView
from delayed_replay.serialization import time_text
from delayed_replay.validation import ReplayContractError

pytestmark = pytest.mark.usefixtures("network_guard")


def setup(tmp_path, *, lane="price", day=date(2024, 8, 9)):
    plan = fixture_plan()
    original = plan.market.snapshots[0]
    if lane == "price":
        missing = tuple(
            b
            for b in original.observations
            if b.symbol == "A" and b.session_date == day
        )
        packet = InputPacket(MarketView((reprice(original, observations=missing),), ()))
        market = replace(
            plan.market,
            snapshots=(
                reprice(
                    original,
                    observations=tuple(
                        b for b in original.observations if b not in missing
                    ),
                ),
            ),
        )
    else:
        source = plan.market.open_snapshots[0]
        missing = tuple(e for e in source.evidence if e.session == day.isoformat())
        packet = InputPacket(MarketView((), (replace(source, evidence=missing),)))
        market = replace(
            plan.market,
            open_snapshots=(
                replace(
                    source,
                    evidence=tuple(e for e in source.evidence if e not in missing),
                ),
            ),
        )
    plan = replace(plan, market=market)
    files = InputArtifactStore(tmp_path / "inputs")
    engine = ExtensibleReplayEngine.create(tmp_path / "replay.sqlite", plan, files)
    return plan, files, engine, packet


def event(engine, packet, key="add", at=WALL):
    return InputExtensionEvent(
        key, engine.input_head, packet.payload.sha256, time_text(at)
    )


@pytest.mark.parametrize(
    "lane,day", [("price", date(2024, 8, 9)), ("open", date(2024, 6, 18))]
)
def test_wait_accept_resume_same_account_and_frozen_measurement(tmp_path, lane, day):
    plan, files, engine, packet = setup(tmp_path, lane=lane, day=day)
    try:
        waiting = engine.run(WALL).to_dict()
        assert waiting["status"] == "waiting_for_input"
        before = engine.store.read()
        frozen = before.current_state.to_dict()["measurements"]
        if lane == "price":
            assert frozen["1"]["equity"] == "227859"
        extension = event(engine, packet)
        files.publish(packet)
        engine.accept(extension)
        accepted = engine.store.read()
        assert engine.state.to_dict() == waiting
        engine.accept(extension)
        assert engine.store.read() == accepted
    finally:
        engine.store.close()
    engine = ExtensibleReplayEngine.resume(
        tmp_path / "replay.sqlite", plan, files, expected_head=accepted.head
    )
    try:
        final = engine.run(WALL).to_dict()
        assert final["status"] == "completed"
        assert len(final["epochs"]) == 3 and len(final["account"]["episodes"]) == 7
        assert final["account"]["valuation"]["display_equity"] == "324487.89"
        after = engine.store.read()
        assert after.events[: len(before.events)] == before.events
        assert all(final["epochs"][k] == v for k, v in waiting["epochs"].items())
        for k, value in frozen.items():
            assert after.current_state.to_dict()["measurements"][k] == value
        measurement = after.current_state.to_dict()["measurements"]["3"]
        assert (
            measurement["completed_trades"] == 7 and measurement["unique_symbols"] == 2
        )
        assert engine.advance(WALL) == "completed"
        assert engine.store.read() == after
    finally:
        engine.store.close()


@pytest.mark.parametrize(
    "point",
    [
        "before_file_write",
        "before_file_publish",
        "after_file_publish",
        "before_event_insert",
        "after_state_update",
        "after_commit",
    ],
)
def test_file_db_failure_and_restart(tmp_path, point):
    plan, files, engine, packet = setup(tmp_path)
    before = engine.store.read()
    extension = event(engine, packet)

    def fault(where):
        if where == point:
            raise RuntimeError("injected_failure")

    try:
        with pytest.raises(RuntimeError, match="injected_failure"):
            files.publish(packet, fault=fault)
            engine.accept(extension, fault=fault)
    finally:
        engine.store.close()
    engine = ExtensibleReplayEngine.resume(tmp_path / "replay.sqlite", plan, files)
    try:
        assert engine.state.to_dict() == before.current_state.to_dict()["replay"]
        assert len(engine.store.read().events) == (1 if point == "after_commit" else 0)
        # Orphans do not activate rows; a retry is explicit and idempotent.
        if point != "after_commit":
            assert engine.input_head == extension.parent_version
        files.publish(packet)
        engine.accept(extension)
        assert len(engine.store.read().events) == 1
        assert not list(files.root.glob(".input-*"))
    finally:
        engine.store.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_accepted_file_damage_refuses_recovery(tmp_path, damage):
    plan, files, engine, packet = setup(tmp_path)
    files.publish(packet)
    engine.accept(event(engine, packet))
    engine.store.close()
    target = files.path(packet.payload.sha256)
    if damage == "missing":
        target.unlink()
    else:
        target.write_text("{}")
    with pytest.raises(ReplayContractError):
        ExtensibleReplayEngine.resume(tmp_path / "replay.sqlite", plan, files)


def test_refetch_revision_parent_conflict_and_unknown_symbol(tmp_path):
    plan, files, engine, packet = setup(tmp_path)
    try:
        files.publish(packet)
        first = event(engine, packet)
        engine.accept(first)
        refetched = InputPacket(
            MarketView(
                (
                    reprice(
                        packet.market.snapshots[0], fetched_at=WALL + timedelta(days=1)
                    ),
                ),
                (),
            )
        )
        files.publish(refetched)
        engine.accept(event(engine, refetched, "refetch", WALL + timedelta(days=1)))
        state = engine.store.read().current_state.to_dict()
        assert state["inputs"]["versions"][engine.input_head]["status"] == "refetched"
        changed = replace(
            packet.market.snapshots[0].observations[0],
            raw_ohlcv=(200.0, 201.0, 199.0, 200.0, 1000.0),
        )
        revision = InputPacket(
            MarketView(
                (reprice(packet.market.snapshots[0], observations=(changed,)),), ()
            )
        )
        files.publish(revision)
        engine.accept(event(engine, revision, "revision", WALL + timedelta(days=1)))
        assert (
            engine.store.read().current_state.to_dict()["inputs"]["versions"][
                engine.input_head
            ]["status"]
            == "quarantined_revision"
        )
        assert (
            engine.store.read().current_state.to_dict()["inputs"]["rows"]
            == state["inputs"]["rows"]
        )
        with pytest.raises(ReplayContractError, match="stale_input_parent"):
            engine.accept(replace(first, extension_id="stale"))
        with pytest.raises(ReplayContractError, match="extension_id_conflict"):
            engine.accept(replace(first, packet_hash=revision.payload.sha256))
        unknown = InputPacket(
            MarketView(
                (
                    reprice(
                        packet.market.snapshots[0],
                        observations=(replace(changed, symbol="UNKNOWN"),),
                    ),
                ),
                (),
            )
        )
        files.publish(unknown)
        with pytest.raises(ReplayContractError, match="symbol"):
            engine.accept(event(engine, unknown, "unknown", WALL + timedelta(days=1)))
        with pytest.raises(ReplayContractError, match="clock"):
            engine.advance(WALL)
    finally:
        engine.store.close()


def test_past_missing_row_is_quarantined_after_selection(tmp_path):
    plan, files, engine, packet = setup(tmp_path, day=date(2024, 5, 10))
    try:
        assert engine.advance(WALL) == "running"  # Selection already confirmed.
        before = engine.state.to_dict()
        files.publish(packet)
        engine.accept(event(engine, packet))
        root = engine.store.read().current_state.to_dict()
        assert (
            root["inputs"]["versions"][engine.input_head]["status"]
            == "quarantined_past"
        )
        assert engine.state.to_dict() == before
    finally:
        engine.store.close()


def test_unsaved_and_future_fetched_inputs_not_accepted(tmp_path):
    _, files, engine, packet = setup(tmp_path)
    try:
        with pytest.raises(ReplayContractError, match="missing_or_corrupt"):
            engine.accept(event(engine, packet))
        later = InputPacket(
            MarketView(
                (
                    reprice(
                        packet.market.snapshots[0], fetched_at=WALL + timedelta(days=1)
                    ),
                ),
                (),
            )
        )
        files.publish(later)
        from delayed_replay.audit_errors import InvalidEvent

        with pytest.raises(InvalidEvent):
            engine.accept(event(engine, later))
        assert not engine.store.read().events
    finally:
        engine.store.close()


def test_mixed_identical_new_rows_keep_original_snapshot_provenance(tmp_path):
    plan, files, engine, _ = setup(tmp_path)
    try:
        packet = InputPacket(fixture_plan().market)
        files.publish(packet)
        engine.accept(event(engine, packet))
        from delayed_replay.clock import ReplayClock

        market = engine._verified_market()
        rows = market.observations(
            ReplayClock(plan.sessions[-1].close_at, WALL),
            date(2024, 4, 1),
            date(2024, 9, 1),
            plan.universe,
        )
        assert len({(b.symbol, b.session_date) for b, _ in rows}) == len(rows)
        old = plan.market.snapshots[0]
        for bar, source in rows:
            if bar.session_date == date(2024, 8, 9) and bar.symbol == "A":
                assert (
                    source.payload_sha256 == packet.market.snapshots[0].payload_sha256
                )
            else:
                assert source.payload_sha256 == old.payload_sha256
        assert (
            engine.run(WALL).to_dict()["account"]["valuation"]["display_equity"]
            == "324487.89"
        )
    finally:
        engine.store.close()


def test_added_corporate_action_stops_without_dropping_position(tmp_path):
    _, files, engine, packet = setup(tmp_path, day=date(2024, 6, 24))
    try:
        waiting = engine.run(WALL).to_dict()
        assert "A" in waiting["account"]["positions"]
        snapshot = packet.market.snapshots[0]
        action = InputPacket(
            MarketView(
                (
                    reprice(
                        snapshot,
                        observations=tuple(
                            replace(b, stock_split=2.0) for b in snapshot.observations
                        ),
                    ),
                ),
                (),
            )
        )
        files.publish(action)
        engine.accept(event(engine, action))
        stopped = engine.run(WALL).to_dict()
        assert stopped["status"] == "stopped_contract"
        assert stopped["account"]["positions"] == waiting["account"]["positions"]
        assert "unsupported_corporate_action" in stopped["reason"]
    finally:
        engine.store.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "yfinance"),
        ("provider", "unknown"),
        ("provider_price_basis", "wrong"),
    ],
)
def test_foreign_provider_or_basis_packet_rejected_as_whole(tmp_path, field, value):
    _, _, engine, packet = setup(tmp_path)
    try:
        payload = packet.payload.to_dict()
        payload["market"]["snapshots"][0][field] = value
        from delayed_replay.serialization import JsonObject

        with pytest.raises(ReplayContractError):
            InputPacket.from_payload(JsonObject.from_value(payload))
        assert not engine.store.read().events
    finally:
        engine.store.close()


def test_base_identity_change_rejected_after_extension(tmp_path):
    plan, files, engine, packet = setup(tmp_path)
    files.publish(packet)
    engine.accept(event(engine, packet))
    engine.store.close()
    from delayed_replay.audit_errors import IdentityMismatch

    with pytest.raises(IdentityMismatch):
        ExtensibleReplayEngine.resume(
            tmp_path / "replay.sqlite", replace(plan, source_identity="changed"), files
        )


def test_concurrent_extension_during_phase_uses_one_read_snapshot(
    tmp_path, monkeypatch
):
    plan, files, engine, packet = setup(tmp_path)
    files.publish(packet)
    other = ExtensibleReplayEngine.resume(tmp_path / "replay.sqlite", plan, files)
    original = engine._verified_market

    def interleave(records=None):
        market = original(records)
        other.accept(event(other, packet, "concurrent"))
        return market

    monkeypatch.setattr(engine, "_verified_market", interleave)
    from delayed_replay.audit_errors import HeadConflict

    try:
        with pytest.raises(HeadConflict):
            engine.advance(WALL)
        records = engine.store.read()
        assert len(records.events) == 1
        assert records.events[0].command.event_type == "input.extension"
        assert not engine.state.to_dict()["epochs"]
    finally:
        other.store.close()
        engine.store.close()
