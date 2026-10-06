"""Artificial clocks/identities/SQLite only; no operational Store or live I/O."""

import inspect
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from test_feasibility_live_store_i1a1 import (
    UTC_TIME,
    add_journal,
    bootstrap,
    committed,
    corrupt,
    document,
    first,
    insert,
    new_connection,
    policy,
    sources,
)

from feasibility import live_store as v1
from feasibility import live_store_runtime as rt
from feasibility.http_contract import HttpContractError

UUID = "12345678-1234-4234-8234-123456789abc"


def sample(seconds=0, mono=None, *, microseconds=0):
    utc = datetime.fromisoformat(UTC_TIME) + timedelta(
        seconds=seconds, microseconds=microseconds
    )
    return rt.ClockObservation(
        utc.isoformat(timespec="microseconds"),
        seconds * 1_000_000_000 if mono is None else mono,
    )


def physical():
    return rt.PhysicalBinding(
        rt.PhysicalIdentity(1, 10, "directory", 3),
        rt.PhysicalIdentity(1, 20, "directory", 2),
        rt.PhysicalIdentity(1, 30, "regular_file", 1),
        rt.PhysicalIdentity(1, 40, "regular_file", 1),
    )


def fixture_context():
    src = sources()
    preflight = src["preflight"]
    registry = document("owner_registry", preflight.registry.to_dict())
    preflight_doc = document("preflight", preflight.to_dict())
    runtime = rt.RuntimePolicy()
    deployment = v1.DeploymentRecord(
        UUID,
        "deployment-a",
        preflight.identity.account_ref,
        registry.sha256,
        preflight_doc.sha256,
        policy().sha256,
        UTC_TIME,
    )
    parent = preflight.canonical_ledger_path.rsplit("/", 1)[0]
    intent = rt.OwnerDeploymentRecord(
        deployment.deployment_id,
        deployment.account_ref,
        registry.sha256,
        preflight_doc.sha256,
        preflight.identity.sha256,
        policy().sha256,
        runtime.sha256,
        preflight.registry.canonical_store_root,
        preflight.canonical_ledger_path,
        parent + "/" + rt.LOCK_FILENAME,
        "bootstrap-a",
        "decision-a",
        "approved",
    )
    return rt.RuntimeContext(deployment, runtime, intent), (registry, preflight_doc)


def append(
    context,
    chain,
    kind,
    *,
    observation=None,
    payload=None,
    session=None,
    epoch=None,
    transition=None,
):
    p = rt.replay_runtime(context, chain)
    return rt.append_runtime_event(
        context,
        chain,
        kind=kind,
        observation=observation or sample(len(chain)),
        transition_id=transition or f"event-{len(chain)}",
        session_id=p.current_session_id if session is None else session,
        fence_epoch=p.fence_epoch if epoch is None else epoch,
        payload=payload or {},
    )


def created():
    context, docs = fixture_context()
    chain = append(
        context,
        (),
        "bootstrap_created",
        observation=sample(),
        payload={
            "intent_sha": context.owner_intent.sha256,
            "physical": physical().to_dict(),
        },
    )
    return context, docs, chain


def pinned():
    context, docs, chain = created()
    pin = replace(
        context.owner_intent,
        state="pinned",
        store_uuid=UUID,
        physical=physical(),
        bootstrap_evidence_sha=rt.replay_runtime(context, chain).head_sha,
        owner_pin_reference="owner-pin-a",
    )
    return replace(context, owner_pins=(pin,)), docs, chain


def finalized():
    context, docs, chain = pinned()
    pin = context.owner_pins[0]
    chain = append(
        context,
        chain,
        "bootstrap_finalized",
        payload={
            "owner_pin_sha": pin.sha256,
            "owner_pin_reference": pin.owner_pin_reference,
            "bootstrap_head": pin.bootstrap_evidence_sha,
            "lifecycle": "prepared",
        },
    )
    return context, docs, chain


def running():
    context, docs, chain = finalized()
    chain = append(
        context,
        chain,
        "session_started",
        session="session-a",
        epoch=1,
        payload={"owner_pin_sha": context.owner_pins[0].sha256},
    )
    return context, docs, chain


def closed():
    context, docs, chain = running()
    chain = append(
        context,
        chain,
        "session_closed",
        payload={
            "predecessor": rt.replay_runtime(context, chain).head_sha,
            "quiescent": True,
            "validation_sha": "a" * 64,
        },
    )
    return context, docs, chain


def init(state=running, path=":memory:"):
    context, docs, chain = state()
    connection = new_connection(path)
    rt._initialize_schema_v2(connection, context, policy(), docs, chain)
    return connection, context, docs, chain


@pytest.mark.parametrize("state", [created, pinned, finalized, running, closed])
def test_v2_round_trip_all_lifecycles(state, tmp_path):
    path = tmp_path / "artificial.sqlite3"
    connection, context, _, chain = init(state, path)
    expected = rt.replay_runtime(context, chain)
    assert rt.validate_store_v2_contents(connection) == expected
    connection.close()
    connection = new_connection(path)
    assert rt.validate_store_v2_contents(connection) == expected
    assert (
        tuple(
            r[0]
            for r in connection.execute(
                "SELECT canonical FROM runtime_events ORDER BY sequence"
            )
        )
        == chain
    )
    connection.close()


def test_policy_canonical_and_approved_values():
    p = rt.RuntimePolicy()
    assert rt.RuntimePolicy.from_bytes(p.canonical, p.sha256) == p
    assert p.drift_threshold_ns == 5_000_000_000
    assert p.supported_platforms == ("darwin", "linux")
    assert p.sqlite_minimum == "3.31.0"
    assert p.macos_fullfsync and not p.linux_fullfsync_required
    assert p.suspend_policy == "unsupported_no_complete_detection_guarantee"
    assert "max_enrolled_plans_per_account" not in p.to_dict()


@pytest.mark.parametrize(
    "field,value",
    [
        ("drift_threshold_ns", 5_000_000_001),
        ("drift_threshold_ns", True),
        ("utc_backward_stop", False),
        ("monotonic_backward_stop", False),
        ("automatic_takeover", True),
        ("automatic_takeover", 0),
        ("journal_mode", "WAL"),
        ("synchronous", "FULL"),
        ("trusted_schema", True),
        ("ignore_check_constraints", True),
        ("foreign_keys", False),
        ("recursive_triggers", False),
        ("busy_timeout_ms", 1),
        ("locking_mode", "EXCLUSIVE"),
        ("supported_platforms", ("windows",)),
        ("elapsed_source", "time"),
        ("utc_source", "caller"),
        ("macos_fullfsync", False),
        ("linux_fullfsync_required", True),
        ("sqlite_minimum", "3.30.0"),
        ("filesystem_policy", "nfs"),
        ("restart_policy", "auto"),
        ("lock_policy", "ttl"),
        ("suspend_policy", "supported"),
    ],
)
def test_policy_rejects_unapproved_values(field, value):
    with pytest.raises(HttpContractError, match="policy_value_invalid"):
        replace(rt.RuntimePolicy(), **{field: value})


@pytest.mark.parametrize("state", [created, pinned])
def test_owner_record_roundtrip(state):
    context, _, _ = state()
    for record in (context.owner_intent, *context.owner_pins):
        assert (
            rt.OwnerDeploymentRecord.from_bytes(record.canonical, record.sha256)
            == record
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("canonical_root", "relative"),
        ("canonical_root", "/var/lib/../unsafe"),
        ("db_path", "relative"),
        ("db_path", "/var/lib/a/../db"),
        ("lock_path", "relative"),
        ("account_ref", "bad/account"),
        ("registry_sha", "wrong"),
        ("policy_approval_status", True),
        ("state", "active"),
        ("store_uuid", UUID),
    ],
)
def test_owner_record_invalid(field, value):
    context, _ = fixture_context()
    with pytest.raises(HttpContractError):
        replace(context.owner_intent, **{field: value})


@pytest.mark.parametrize(
    "role,field,value",
    [
        ("db", "link_count", 2),
        ("lock", "link_count", 2),
        ("db", "object_kind", "directory"),
        ("root", "object_kind", "regular_file"),
        ("lock", "inode", 30),
        ("root", "object_kind", "symlink"),
        ("db", "device", -1),
        ("db", "inode", 0),
        ("db", "inode", True),
        ("db", "link_count", float("nan")),
    ],
)
def test_physical_value_invalid(role, field, value):
    p = physical()
    with pytest.raises(HttpContractError):
        replace(p, **{role: replace(getattr(p, role), **{field: value})})


@pytest.mark.parametrize(
    "field",
    [
        "runtime_policy_sha",
        "store_policy_sha",
        "registry_sha",
        "preflight_sha",
        "deployment_id",
        "account_ref",
    ],
)
def test_context_binding_mismatch(field):
    context, _ = fixture_context()
    value = "f" * 64 if field.endswith("sha") else "other"
    if field == "account_ref":
        # A syntactically valid alternate account/path must still fail binding.
        root = context.owner_intent.canonical_root + "/accounts/other/"
        intent = replace(
            context.owner_intent,
            account_ref=value,
            db_path=root + rt.LIVE_LEDGER_FILENAME,
            lock_path=root + rt.LOCK_FILENAME,
        )
    else:
        intent = replace(context.owner_intent, **{field: value})
    with pytest.raises(HttpContractError, match="owner_deployment_binding_mismatch"):
        replace(context, owner_intent=intent)


def test_pending_is_not_prepared_or_autofinalized():
    context, _, chain = created()
    p = rt.replay_runtime(context, chain)
    assert p.lifecycle == "bootstrap_pending" and p.fence_epoch == 0
    assert rt.assess_startup(context, chain).classification == "bootstrap_incomplete"
    assert rt.replay_runtime(context, chain) == p
    with pytest.raises(HttpContractError, match="bootstrap_not_prepared"):
        append(
            context,
            chain,
            "session_started",
            session="a",
            epoch=1,
            payload={"owner_pin_sha": "a" * 64},
        )


@pytest.mark.parametrize(
    "change", ["missing", "digest", "uuid", "physical", "head", "reference"]
)
def test_bad_pin_cannot_finalize(change):
    context, _, chain = pinned()
    pin = context.owner_pins[0]
    payload = {
        "owner_pin_sha": pin.sha256,
        "owner_pin_reference": pin.owner_pin_reference,
        "bootstrap_head": pin.bootstrap_evidence_sha,
        "lifecycle": "prepared",
    }
    with pytest.raises(HttpContractError):
        if change == "missing":
            context = replace(context, owner_pins=())
        elif change == "digest":
            payload["owner_pin_sha"] = "b" * 64
        elif change in ("uuid", "physical"):
            pin = replace(
                pin,
                **(
                    {"store_uuid": "22345678-1234-4234-8234-123456789abc"}
                    if change == "uuid"
                    else {
                        "physical": replace(
                            physical(), db=rt.PhysicalIdentity(1, 50, "regular_file", 1)
                        )
                    }
                ),
            )
            context = replace(context, owner_pins=(pin,))
            payload["owner_pin_sha"] = pin.sha256
        elif change == "head":
            payload["bootstrap_head"] = "b" * 64
        else:
            payload["owner_pin_reference"] = "wrong"
        append(context, chain, "bootstrap_finalized", payload=payload)


def test_finalize_before_create_and_double_finalize():
    context, _, chain = finalized()
    event = rt.RuntimeEvent.from_bytes(chain[-1])
    with pytest.raises(HttpContractError, match="bootstrap_missing"):
        append(context, (), "bootstrap_finalized", payload=v1._load(event.payload))
    with pytest.raises(HttpContractError, match="bootstrap_not_pending"):
        append(context, chain, "bootstrap_finalized", payload=v1._load(event.payload))


@pytest.mark.parametrize(
    "field,value",
    [
        ("previous_hash", "f" * 64),
        ("event_hash", "f" * 64),
        ("sequence", 9),
        ("sequence", True),
        ("kind", "clear"),
        ("kind", []),
        ("store_id", "22345678-1234-4234-8234-123456789abc"),
        ("deployment_id", "other"),
        ("runtime_policy_sha", "f" * 64),
        ("schema", "unknown"),
        ("fence_epoch", -1),
    ],
)
def test_runtime_chain_adversarial(field, value):
    context, _, chain = running()
    raw = json.loads(chain[-1])
    raw[field] = value
    if field != "event_hash":
        raw["event_hash"] = v1.digest_bytes(
            v1.canonical_bytes({k: v for k, v in raw.items() if k != "event_hash"})
        )
    with pytest.raises(HttpContractError):
        rt.replay_runtime(context, (*chain[:-1], v1.canonical_bytes(raw)))


@pytest.mark.parametrize("epoch", [0, 2, -1, True, 2**63])
def test_fence_skip_decrement_and_type_rejected(epoch):
    context, _, chain = finalized()
    with pytest.raises(HttpContractError):
        append(
            context,
            chain,
            "session_started",
            session="a",
            epoch=epoch,
            payload={"owner_pin_sha": context.owner_pins[0].sha256},
        )


def test_epoch_overflow_guard_without_impossible_history():
    context, _, chain = finalized()
    p = replace(rt.replay_runtime(context, chain), fence_epoch=rt.MAX_INTEGER)
    event = rt.RuntimeEvent(
        "session_started",
        len(chain),
        p.head_sha,
        context.deployment.deployment_id,
        UUID,
        context.runtime_policy.sha256,
        "new",
        rt.MAX_INTEGER,
        sample(2),
        "new",
        v1.canonical_bytes({"owner_pin_sha": p.owner_pin_sha}),
    )
    with pytest.raises(HttpContractError, match="fence_overflow"):
        rt._reduce(context, p, event)


def test_clean_restart_new_epoch_unique_session_and_reset_mono():
    context, _, chain = closed()
    assert rt.assess_startup(context, chain).classification == "new_session_candidate"
    with pytest.raises(HttpContractError, match="session_id_reused"):
        append(
            context,
            chain,
            "session_started",
            session="session-a",
            epoch=2,
            payload={"owner_pin_sha": context.owner_pins[0].sha256},
        )
    next_chain = append(
        context,
        chain,
        "session_started",
        session="session-b",
        epoch=2,
        observation=sample(4, mono=0),
        payload={"owner_pin_sha": context.owner_pins[0].sha256},
    )
    p = rt.replay_runtime(context, next_chain)
    assert p.fence_epoch == 2 and p.last_monotonic_ns == 0
    assert p.last_clean_session == "session-a" and len(p.sessions) == 2


@pytest.mark.parametrize(
    "utc", ["2026-01-01", "2026-01-01T00:00:00", "2026-01-01T00:00:00+09:00", None]
)
def test_clock_utc_exact(utc):
    with pytest.raises(HttpContractError):
        rt.ClockObservation(utc, 0)


@pytest.mark.parametrize("mono", [True, -1, 1.0, float("nan"), "1", 2**63])
def test_clock_monotonic_type(mono):
    with pytest.raises(HttpContractError):
        rt.ClockObservation(UTC_TIME, mono)


@pytest.mark.parametrize(
    "drift_us", [-5_000_001, -5_000_000, -4_999_999, 4_999_999, 5_000_000, 5_000_001]
)
def test_integer_clock_boundaries(drift_us):
    context, _, chain = running()
    # Both clocks advance; negative drift never also means UTC backwards here.
    observed = sample(22, mono=22_000_000_000, microseconds=drift_us)
    updated = rt.record_clock_observation(context, chain, observed, "clock-a")
    p = rt.replay_runtime(context, updated)
    stop = abs(drift_us) > 5_000_000
    assert p.persistent_stop is stop
    assert p.clock_uncertain is stop
    assert rt.RuntimeEvent.from_bytes(updated[-1]).kind == (
        "runtime_stop_entered" if stop else "clock_observed"
    )
    assert (p.last_accepted_utc == observed.utc) is not stop


@pytest.mark.parametrize(
    "observed,reason",
    [
        (sample(2, mono=2_000_000_001, microseconds=-1), "utc_backward"),
        (sample(2, mono=1_999_999_999), "monotonic_backward"),
        (sample(32, mono=3_000_000_000), "drift_exceeded"),
    ],
)
def test_clock_backward_and_artificial_suspend(observed, reason):
    context, _, chain = running()
    updated = rt.record_clock_observation(context, chain, observed, "clock-stop")
    event = rt.RuntimeEvent.from_bytes(updated[-1])
    assert reason in v1._load(event.payload)["clock_reasons"]
    assert event.observation == observed  # Original backward UTC is preserved.
    assert rt.replay_runtime(context, updated).persistent_stop


def test_equal_utc_does_not_invent_microsecond():
    context, _, chain = running()
    observed = sample(2, mono=2_000_000_001)
    updated = rt.record_clock_observation(context, chain, observed, "equal-utc")
    p = rt.replay_runtime(context, updated)
    assert p.last_accepted_utc == sample(2).utc and not p.persistent_stop
    assert rt.RuntimeEvent.from_bytes(updated[-1]).observation.utc == observed.utc


@pytest.mark.parametrize(
    "kind",
    [
        "session_started",
        "session_closed",
        "clock_observed",
        "bootstrap_finalized",
        "runtime_stop_entered",
    ],
)
def test_sticky_stop_cannot_be_cleared(kind):
    context, _, chain = running()
    chain = rt.record_clock_observation(
        context, chain, sample(20, mono=3_000_000_000), "stop"
    )
    assert rt.assess_startup(context, chain).classification == "manual_blocked"
    with pytest.raises(HttpContractError, match="persistent_stop"):
        append(
            context, chain, kind, observation=sample(21), session="session-b", epoch=2
        )


@pytest.mark.parametrize(
    "kind", ["session_closed", "runtime_stop_entered", "clock_observed"]
)
@pytest.mark.parametrize("wrong", ["session", "epoch"])
def test_runtime_wrong_session_or_epoch(kind, wrong):
    context, _, chain = running()
    with pytest.raises(HttpContractError, match="session_mismatch|fence_mismatch"):
        append(
            context,
            chain,
            kind,
            session="wrong" if wrong == "session" else "session-a",
            epoch=2 if wrong == "epoch" else 1,
        )


@pytest.mark.parametrize(
    "state,expected",
    [
        (created, "bootstrap_incomplete"),
        (finalized, "new_session_candidate"),
        (running, "manual_blocked"),
        (closed, "new_session_candidate"),
    ],
)
def test_restart_assessment(state, expected):
    context, _, chain = state()
    assert rt.assess_startup(context, chain).classification == expected
    if state == running:
        assert rt.assess_startup(context, chain).reasons == ("restart_uncertain",)
        with pytest.raises(HttpContractError, match="dirty_restart"):
            append(
                context,
                chain,
                "session_started",
                session="new",
                epoch=2,
                payload={"owner_pin_sha": context.owner_pins[0].sha256},
            )


@pytest.mark.parametrize("backward", [False, True])
def test_dirty_restart_evidence_has_new_process_monotonic(backward):
    context, _, chain = running()
    observation = sample(1 if backward else 3, mono=0)
    chain = append(
        context,
        chain,
        "runtime_stop_entered",
        observation=observation,
        payload={
            "reason": "restart_uncertain",
            "clock_reasons": ["utc_backward"] if backward else [],
            "evidence_reference": "new-process",
        },
    )
    p = rt.replay_runtime(context, chain)
    assert p.restart_uncertain and p.persistent_stop
    assert p.clock_uncertain is backward
    assert p.last_monotonic_ns == 2_000_000_000
    assert rt.assess_startup(context, chain).classification == "manual_blocked"


@pytest.mark.parametrize(
    "field,value",
    [
        ("quiescent", False),
        ("quiescent", 1),
        ("validation_sha", "bad"),
        ("predecessor", "a" * 64),
    ],
)
def test_clean_close_requires_bound_evidence(field, value):
    context, _, chain = running()
    payload = {
        "quiescent": True,
        "validation_sha": "a" * 64,
        "predecessor": rt.replay_runtime(context, chain).head_sha,
    }
    payload[field] = value
    with pytest.raises(HttpContractError):
        append(context, chain, "session_closed", payload=payload)


def test_event_after_close_and_reused_transition_rejected():
    context, _, chain = closed()
    with pytest.raises(HttpContractError, match="session_not_running"):
        append(context, chain, "clock_observed")
    context, _, chain = running()
    with pytest.raises(HttpContractError, match="transition_id_reused"):
        append(context, chain, "clock_observed", transition="event-1")


def test_v1_and_v2_are_separate_no_migration():
    old = new_connection()
    bootstrap(old)
    before = tuple(old.iterdump())
    assert v1.validate_store_contents(old) == v1.STORE_SCHEMA
    with pytest.raises(HttpContractError, match="schema_definition_mismatch"):
        rt.validate_store_v2_contents(old)
    context, docs, chain = created()
    with pytest.raises(HttpContractError, match="existing_database_no_migration"):
        rt._initialize_schema_v2(old, context, policy(), docs, chain)
    assert tuple(old.iterdump()) == before
    new, _, _, _ = init()
    with pytest.raises(HttpContractError, match="schema_definition_mismatch"):
        v1.validate_store_contents(new)
    old.close()
    new.close()


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE runtime_current SET canonical=x'7b7d'",
        "UPDATE runtime_current SET event_count=2,last_sequence=1,head_sha=(SELECT event_hash FROM runtime_events WHERE sequence=1)",
        "UPDATE runtime_sessions SET canonical=x'7b7d'",
        "UPDATE runtime_sessions SET fence_epoch=2",
        "DELETE FROM runtime_sessions",
        "DELETE FROM runtime_current",
    ],
)
def test_projection_tamper_detected_without_repair(tamper):
    connection, _, _, _ = init()
    connection.execute(tamper)
    before = tuple(connection.iterdump())
    with pytest.raises(HttpContractError, match="projection"):
        rt.validate_store_v2_contents(connection)
    assert tuple(connection.iterdump()) == before
    connection.close()


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TRIGGER runtime_events_no_delete",
        "CREATE TABLE extra (x)",
        "CREATE INDEX extra ON runtime_events(event_hash)",
        "CREATE VIEW extra AS SELECT * FROM runtime_events",
        "ALTER TABLE runtime_sessions ADD COLUMN extra TEXT",
    ],
)
def test_v2_schema_drift_rejected(sql):
    connection, _, _, _ = init()
    connection.execute(sql)
    with pytest.raises(HttpContractError, match="schema_definition_mismatch"):
        rt.validate_store_v2_contents(connection)
    connection.close()


@pytest.mark.parametrize(
    "table",
    [
        "runtime_events",
        "runtime_policies",
        "runtime_bindings",
        "owner_deployment_records",
    ],
)
@pytest.mark.parametrize("operation", ["delete", "update", "replace"])
def test_runtime_originals_immutable(table, operation):
    connection, _, _, _ = init()
    column = connection.execute(f"PRAGMA table_info({table})").fetchone()[1]
    sql = {
        "delete": f"DELETE FROM {table}",
        "update": f"UPDATE {table} SET {column}={column}",
        "replace": f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}",
    }[operation]
    before = tuple(connection.iterdump())
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql)
    assert tuple(connection.iterdump()) == before
    connection.close()


def test_bad_documents_roll_back_entire_fresh_schema():
    connection = new_connection()
    context, _, chain = created()
    with pytest.raises(HttpContractError):
        rt._initialize_schema_v2(connection, context, policy(), (), chain)
    assert not connection.execute("SELECT name FROM sqlite_master").fetchall()
    connection.close()


def test_runtime_backward_utc_persisted_but_business_rule_retained():
    context, docs, chain = running()
    chain = rt.record_clock_observation(
        context, chain, sample(1, mono=3_000_000_000), "stop"
    )
    connection = new_connection()
    rt._initialize_schema_v2(connection, context, policy(), docs, chain)
    assert rt.validate_store_v2_contents(connection).clock_uncertain
    # Rehashed backward C2 business UTC must still fail in this same v2 Store.
    d = context.deployment
    insert(
        connection,
        "accounts",
        deployment_id=d.deployment_id,
        account_ref=d.account_ref,
        schema=v1.LIVE_ACCOUNT_SCHEMA_V2,
        policy_sha=policy().sha256,
        enrollment_revision=0,
        request_generation=0,
        status="prepared",
    )
    journal = first().account
    raw = json.loads(journal.records[1])
    raw["event"]["recorded_at"] = sample(0, microseconds=-1).utc
    raw["event_hash"] = v1.digest_bytes(
        v1.canonical_bytes({k: v for k, v in raw.items() if k != "event_hash"})
    )
    changed = SimpleNamespace(
        schema=journal.schema,
        records=(journal.records[0], v1.canonical_bytes(raw).decode()),
        head_sha=raw["event_hash"],
    )
    add_journal((connection, sources(), d), changed, "account-journal", "account")
    with pytest.raises(HttpContractError, match="event_clock_regressed"):
        rt.validate_store_v2_contents(connection)
    connection.close()


def test_no_operational_surface_or_physical_calls():
    source = inspect.getsource(rt)
    for forbidden in (
        "sqlite3.connect",
        "time.time(",
        "datetime.now(",
        "os.stat(",
        "os.open(",
        "fcntl",
        "getenv",
        "socket",
        "requests.",
    ):
        assert forbidden not in source
    for name in (
        "open_store",
        "StoreSession",
        "clear_stop",
        "activate",
        "acquire_lock",
    ):
        assert not hasattr(rt, name)


def test_v2_preserves_business_journals_and_receipts():
    connection, context, _, _ = init()
    d = context.deployment
    insert(
        connection,
        "accounts",
        deployment_id=d.deployment_id,
        account_ref=d.account_ref,
        schema=v1.LIVE_ACCOUNT_SCHEMA_V2,
        policy_sha=policy().sha256,
        enrollment_revision=0,
        request_generation=0,
        status="prepared",
    )
    _, receipt = committed((connection, sources(), d))
    before = tuple(connection.iterdump())
    assert rt.validate_store_v2_contents(connection).session_state == "running"
    assert connection.execute("SELECT canonical FROM receipts").fetchone()[0] == receipt
    assert tuple(connection.iterdump()) == before
    connection.close()


@pytest.mark.parametrize(
    "field,value",
    [("event_hash", "f" * 64), ("kind", "cleared"), ("observed_monotonic_ns", True)],
)
def test_persisted_event_corruption_fails_without_repair(field, value):
    connection, _, _, chain = init()
    raw = json.loads(chain[-1])
    raw[field] = value
    corrupt(
        connection,
        "runtime_events_no_update",
        "UPDATE runtime_events SET canonical=? WHERE sequence=2",
        (v1.canonical_bytes(raw),),
    )
    before = tuple(connection.iterdump())
    with pytest.raises(HttpContractError):
        rt.validate_store_v2_contents(connection)
    assert tuple(connection.iterdump()) == before
    connection.close()


@pytest.mark.parametrize("wrong", ["physical", "evidence"])
def test_unused_pinned_record_must_match_created_evidence(wrong):
    context, _, chain = pinned()
    pin = context.owner_pins[0]
    if wrong == "physical":
        pin = replace(
            pin,
            physical=replace(
                physical(), db=rt.PhysicalIdentity(1, 99, "regular_file", 1)
            ),
        )
    else:
        pin = replace(pin, bootstrap_evidence_sha="b" * 64)
    with pytest.raises(HttpContractError, match="bootstrap_pin_mismatch"):
        rt.replay_runtime(replace(context, owner_pins=(pin,)), chain)


@pytest.mark.parametrize("target", ["policy", "owner", "event"])
def test_unknown_version_or_noncanonical_runtime_records_rejected(target):
    context, _, chain = pinned()
    data, parser = {
        "policy": (context.runtime_policy.canonical, rt.RuntimePolicy.from_bytes),
        "owner": (context.owner_intent.canonical, rt.OwnerDeploymentRecord.from_bytes),
        "event": (chain[0], rt.RuntimeEvent.from_bytes),
    }[target]
    with pytest.raises(HttpContractError):
        parser(data + b"\n")
    raw = json.loads(data)
    raw["schema"] = "unknown"
    with pytest.raises(HttpContractError):
        parser(v1.canonical_bytes(raw))


@pytest.mark.parametrize("pragma", ["foreign_keys", "recursive_triggers"])
def test_v2_initializer_connection_prerequisites(pragma):
    connection = new_connection()
    connection.execute(f"PRAGMA {pragma}=OFF")
    context, docs, chain = created()
    with pytest.raises(HttpContractError, match=pragma + "_required"):
        rt._initialize_schema_v2(connection, context, policy(), docs, chain)
    assert not connection.execute("SELECT name FROM sqlite_master").fetchall()
    connection.close()


def test_bad_identity_claim_rolls_back_artificial_bootstrap():
    context, docs, chain = created()
    intent = replace(context.owner_intent, identity_sha="c" * 64)
    context = replace(context, owner_intent=intent)
    chain = append(
        context,
        (),
        "bootstrap_created",
        observation=sample(),
        payload={"intent_sha": intent.sha256, "physical": physical().to_dict()},
    )
    connection = new_connection()
    with pytest.raises(HttpContractError, match="identity_binding_mismatch"):
        rt._initialize_schema_v2(connection, context, policy(), docs, chain)
    assert not connection.execute("SELECT name FROM sqlite_master").fetchall()
    connection.close()


def test_commit_after_start_without_delivered_handle_stays_dirty(tmp_path):
    path = tmp_path / "interrupted.sqlite3"
    connection, _, _, _ = init(running, path)
    connection.close()
    connection = new_connection(path)
    assert rt.validate_store_v2_contents(connection).session_state == "running"
    context, _, chain = running()
    assert rt.assess_startup(context, chain).reasons == ("restart_uncertain",)
    connection.close()


def test_clock_stop_cannot_be_hidden_in_normal_observation():
    context, _, chain = running()
    with pytest.raises(HttpContractError, match="clock_stop_required"):
        append(
            context, chain, "clock_observed", observation=sample(20, mono=3_000_000_000)
        )
