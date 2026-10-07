"""Artificial physical Stores only; real OS locks/SQLite/clocks, no network."""

import inspect
import multiprocessing as mp
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import warnings
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from test_feasibility_live_preflight import preflight, registry
from test_feasibility_live_store_i1a1 import policy

from feasibility import live_store as store
from feasibility import live_store_operational as op
from feasibility import live_store_runtime as rt
from feasibility.http_contract import HttpContractError


@pytest.fixture
def authority():
    # C1 rejects system temp roots and repo roots. A private artificial directory
    # in HOME exercises the production boundary without weakening C1 validation.
    with tempfile.TemporaryDirectory(
        prefix="i1a2b-artificial-", dir=Path.home()
    ) as root:
        p = preflight(account_registry=registry(canonical_store_root=root))
        parent = Path(p.canonical_ledger_path).parent
        parent.mkdir(parents=True, mode=0o700)
        parent.parent.chmod(0o700)
        rp, sp = rt.RuntimePolicy(), policy()
        intent = rt.OwnerDeploymentRecord(
            "deployment-artificial",
            p.identity.account_ref,
            p.registry.sha256,
            p.sha256,
            p.identity.sha256,
            sp.sha256,
            rp.sha256,
            root,
            p.canonical_ledger_path,
            str(parent / rt.LOCK_FILENAME),
            "artificial-intent",
            "artificial-policy-decision",
            "approved",
        )
        yield op.OperationalAuthority(p, intent, sp, rp)


def pin(a, candidate):
    return replace(
        a,
        pin=replace(
            a.intent,
            state="pinned",
            store_uuid=candidate.store_uuid,
            physical=candidate.physical,
            bootstrap_evidence_sha=candidate.bootstrap_event_sha,
            owner_pin_reference="artificial-owner-pin",
        ),
    )


@pytest.fixture
def pending(authority):
    return pin(authority, op.create_bootstrap(authority))


@pytest.fixture
def prepared(pending):
    op.finalize_bootstrap(pending)
    return pending


def read(a):
    c = sqlite3.connect(Path(a.intent.db_path).as_uri() + "?mode=ro", uri=True)
    try:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA recursive_triggers=ON")
        p = rt.validate_store_v2_contents(c)
        records = tuple(
            row[0]
            for row in c.execute(
                "SELECT canonical FROM runtime_events ORDER BY sequence"
            )
        )
        return p, records
    finally:
        c.close()


def _worker(a, mode, pipe):
    # spawn-based process. Explicit pipe handshake, never sleeps for locking.
    try:
        warnings.simplefilter("error")
        if mode == "fork-probe":
            _fork_probe(a)
            pipe.send(("fork-ok", None))
            return
        if mode.startswith("crash-"):
            target = mode.removeprefix("crash-")

            def crash(stage):
                if stage == target:
                    pipe.send(("stage", stage))
                    pipe.recv()
                    os._exit(37)

            op._stage = crash
        if mode == "bootstrap":
            pipe.send(("ready", None))
            pipe.recv()
            candidate = op.create_bootstrap(a)
            pipe.send(("created", candidate.store_uuid))
            return
        s = op.open_store(a)
        if mode == "crash-session_closed_committed":
            s.close()
            return
        pipe.send(("opened", s.identity.fence_epoch))
        command = pipe.recv()
        if command == "dirty":
            os._exit(38)
        s.close()
        pipe.send(("closed", None))
    except Exception as exc:
        pipe.send(("error", str(exc)))
    finally:
        pipe.close()


def start(a, mode):
    ctx = mp.get_context("spawn")
    parent, child = ctx.Pipe()
    p = ctx.Process(target=_worker, args=(a, mode, child))
    p.start()
    child.close()
    return p, parent


def receive(pipe):
    assert pipe.poll(30), "subprocess failed to reach synchronization point"
    return pipe.recv()


def finish(p, pipe, expected=0):
    p.join(30)
    if p.is_alive():
        p.terminate()
        p.join(10)
        pytest.fail("artificial subprocess failed to exit")
    pipe.close()
    assert p.exitcode == expected


def test_bootstrap_candidate_and_explicit_finalize(authority):
    candidate = op.create_bootstrap(authority)
    assert candidate.canonical == candidate.canonical
    assert candidate.owner_intent_sha == authority.intent.sha256
    assert candidate.physical.db.link_count == 1
    assert authority.pin is None
    a = pin(authority, candidate)
    before = read(a)
    assert before[0].lifecycle == "bootstrap_pending"
    with pytest.raises(HttpContractError, match="explicit_finalization_required"):
        op.open_store(a)
    assert read(a) == before
    assert op.finalize_bootstrap(a).lifecycle == "prepared"
    with pytest.raises(HttpContractError, match="bootstrap_not_pending"):
        op.finalize_bootstrap(a)


def test_real_clock_pragmas_clean_reopen(prepared):
    a = prepared
    s = op.open_store(a)
    assert s.status == "running" and s.identity.fence_epoch == 1
    assert s.checkpoint_clock().session_state == "running"
    op._check_pragmas(s._resources.connection)
    if sys.platform == "darwin":
        assert s._resources.connection.execute("PRAGMA fullfsync").fetchone() == (1,)
    for fd in s._resources.fds.values():
        assert not os.get_inheritable(fd)
    first = s.identity
    s.close()
    s.close()
    assert s.status == "closed"
    assert Path(a.intent.lock_path).exists()
    s2 = op.open_store(a)
    assert s2.identity.fence_epoch == 2
    assert s2.identity.session_id != first.session_id
    s2.close()
    p, records = read(a)
    assert p.session_state == "clean_closed"
    payload = store._load(rt.RuntimeEvent.from_bytes(records[-1]).payload)
    assert len(payload["validation_sha"]) == 64 and payload["quiescent"] is True


def test_runtime_schema_unchanged(prepared):
    with closing(sqlite3.connect(prepared.intent.db_path)) as c:
        sql = sorted(
            r[0]
            for r in c.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
            )
        )
    assert sql == sorted(rt._DDL_V2)


def test_public_surface(prepared):
    for name in ("create_bootstrap", "finalize_bootstrap", "open_store"):
        assert tuple(inspect.signature(getattr(op, name)).parameters) == ("authority",)
    s = op.open_store(prepared)
    try:
        assert {n for n in dir(s) if not n.startswith("_")} == {
            "identity",
            "status",
            "checkpoint_clock",
            "close",
        }
        with pytest.raises(FrozenInstanceError):
            s.identity.fence_epoch = 50
        with pytest.raises(AttributeError):
            s.status = "closed"
    finally:
        s.close()


@pytest.mark.parametrize("value", [None, {}, "path", Path("/arbitrary")])
def test_arbitrary_authority_rejected(value):
    with pytest.raises(HttpContractError, match="authority_required"):
        op.open_store(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_ref", "other-account"),
        ("deployment_id", "other-deployment"),
        ("store_policy_sha", "a" * 64),
        ("runtime_policy_sha", "a" * 64),
        ("registry_sha", "a" * 64),
        ("preflight_sha", "a" * 64),
    ],
)
def test_owner_mismatch(prepared, field, value):
    before = read(prepared)
    with pytest.raises(HttpContractError):
        altered = replace(prepared.pin, **{field: value})
        op.open_store(replace(prepared, pin=altered))
    assert read(prepared) == before


def test_uuid_mismatch_no_write(prepared):
    before = read(prepared)
    a = replace(
        prepared,
        pin=replace(prepared.pin, store_uuid="00000000-0000-4000-8000-000000000001"),
    )
    with pytest.raises(HttpContractError, match="owner_pin_mismatch"):
        op.open_store(a)
    assert read(prepared) == before


@pytest.mark.parametrize("role", ["db", "lock"])
def test_missing_normal_file_not_created(prepared, role):
    path = Path(getattr(prepared.intent, role + "_path"))
    path.rename(path.with_suffix(".held"))
    with pytest.raises(FileNotFoundError):
        op.open_store(prepared)
    assert not path.exists()


@pytest.mark.parametrize("role", ["db", "lock"])
def test_hardlink_rejected(prepared, role):
    path = Path(getattr(prepared.intent, role + "_path"))
    os.link(path, path.with_suffix(".link"))
    with pytest.raises(HttpContractError, match="hardlink"):
        op.open_store(prepared)


@pytest.mark.parametrize("role", ["root", "account", "db", "lock"])
def test_symlink_rejected(prepared, role):
    path = Path(
        prepared.intent.canonical_root
        if role == "root"
        else str(Path(prepared.intent.db_path).parent)
        if role == "account"
        else getattr(prepared.intent, role + "_path")
    )
    moved = path.with_name(path.name + "-original")
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=moved.is_dir())
    try:
        with pytest.raises((OSError, HttpContractError)):
            op.open_store(prepared)
    finally:
        path.unlink()
        moved.rename(path)


@pytest.mark.parametrize("role", ["db", "lock"])
def test_inode_copy_replacement(prepared, role):
    path = Path(getattr(prepared.intent, role + "_path"))
    replacement = path.with_suffix(".replacement")
    shutil.copyfile(path, replacement)
    replacement.chmod(0o600)
    replacement.replace(path)
    with pytest.raises(HttpContractError, match="physical_pin_mismatch"):
        op.open_store(prepared)


@pytest.mark.parametrize("role", ["root", "account", "db", "lock"])
def test_writable_custody_rejected(prepared, role):
    path = Path(
        prepared.intent.canonical_root
        if role == "root"
        else str(Path(prepared.intent.db_path).parent)
        if role == "account"
        else getattr(prepared.intent, role + "_path")
    )
    prior = path.stat().st_mode
    path.chmod(prior | 0o020)
    try:
        with pytest.raises(HttpContractError, match="custody_invalid"):
            op.open_store(prepared)
    finally:
        path.chmod(prior)


@pytest.mark.parametrize("role", ["db", "lock"])
def test_replacement_while_running_no_clean_marker(prepared, role):
    s = op.open_store(prepared)
    path = Path(getattr(prepared.intent, role + "_path"))
    original = path.with_suffix(".original")
    path.rename(original)
    shutil.copyfile(original, path)
    path.chmod(0o600)
    with pytest.raises(HttpContractError, match="physical"):
        s.checkpoint_clock()
    assert s.status == "invalid"
    assert not s._resources.fds
    # Restore the same inode only in this artificial test, not via production API.
    path.unlink()
    original.rename(path)
    with pytest.raises(HttpContractError, match="restart_uncertain"):
        op.open_store(prepared)


@pytest.mark.parametrize("existing", ["empty", "partial"])
def test_existing_db_never_initialized(authority, existing):
    path = Path(authority.intent.db_path)
    content = b"" if existing == "empty" else b"partial-artificial-evidence"
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(FileExistsError):
        op.create_bootstrap(authority)
    assert path.read_bytes() == content


def test_bootstrap_twice_preserves_uuid(pending):
    before = read(pending)
    with pytest.raises(FileExistsError):
        op.create_bootstrap(replace(pending, pin=None))
    assert read(pending) == before


def test_wal_not_converted(prepared):
    with closing(sqlite3.connect(prepared.intent.db_path)) as c:
        assert c.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    with pytest.raises(HttpContractError, match="journal_mode_invalid"):
        op.open_store(prepared)
    with closing(sqlite3.connect(prepared.intent.db_path)) as c:
        assert c.execute("PRAGMA journal_mode").fetchone() == ("wal",)


@pytest.mark.parametrize(
    "key,value",
    [
        ("synchronous", 0),
        ("foreign_keys", 0),
        ("recursive_triggers", 0),
        ("trusted_schema", 1),
        ("ignore_check_constraints", 1),
        ("busy_timeout", 5),
    ],
)
def test_connection_pragma_tamper_rejected(prepared, key, value):
    s = op.open_store(prepared)
    s._resources.connection.execute(f"PRAGMA {key}={value}")
    with pytest.raises(HttpContractError, match="pragma_"):
        s.checkpoint_clock()
    assert s.status == "invalid"
    with pytest.raises(HttpContractError, match="restart_uncertain"):
        op.open_store(prepared)


def test_production_capture_order(monkeypatch):
    calls = []
    values = iter((10, 15))

    def mono():
        calls.append("mono")
        return next(values)

    class Clock:
        @staticmethod
        def now(tz):
            calls.append("utc")
            return datetime(2026, 1, 1, tzinfo=tz)

    monkeypatch.setattr(op.time, "monotonic_ns", mono)
    monkeypatch.setattr(op, "datetime", Clock)
    sample = op._capture_clock()
    assert calls == ["mono", "utc", "mono"]
    assert sample.monotonic_ns == 15


@pytest.mark.parametrize("drift", [-6, 6])
def test_clock_stop_sticky(prepared, monkeypatch, drift):
    s = op.open_store(prepared)
    p, _ = read(prepared)
    observed = rt.ClockObservation(
        (
            datetime.fromisoformat(p.last_accepted_utc) + timedelta(seconds=10 + drift)
        ).isoformat(timespec="microseconds"),
        p.last_monotonic_ns + 10_000_000_000,
    )
    monkeypatch.setattr(op, "_capture_clock", lambda: observed)
    with pytest.raises(HttpContractError, match="clock_uncertain"):
        s.checkpoint_clock()
    assert read(prepared)[0].persistent_stop
    with pytest.raises(HttpContractError, match="persistent_stop"):
        op.open_store(prepared)


def test_stop_persist_failure_leaves_dirty(prepared, monkeypatch):
    s = op.open_store(prepared)
    observed = rt.ClockObservation("2000-01-01T00:00:00.000000+00:00", 1)

    def fail(*args):
        raise sqlite3.OperationalError("artificial failed stop write")

    with monkeypatch.context() as patch:
        patch.setattr(op, "_capture_clock", lambda: observed)
        patch.setattr(op, "_persist", fail)
        with pytest.raises(sqlite3.OperationalError):
            s.checkpoint_clock()
    assert read(prepared)[0].session_state == "running"
    with pytest.raises(HttpContractError, match="session_not_usable"):
        s.close()
    with pytest.raises(HttpContractError, match="restart_uncertain"):
        op.open_store(prepared)


def test_startup_backward_persistent_stop(prepared, monkeypatch):
    observed = rt.ClockObservation("2000-01-01T00:00:00.000000+00:00", 1)
    monkeypatch.setattr(op, "_capture_clock", lambda: observed)
    with pytest.raises(HttpContractError, match="clock_uncertain"):
        op.open_store(prepared)
    assert read(prepared)[0].persistent_stop


def test_quiescence_not_overclaimed(prepared):
    s = op.open_store(prepared)
    c = s._resources.connection
    from feasibility.live_http_evidence import LIVE_ACCOUNT_SCHEMA_V2

    c.execute(
        "INSERT INTO accounts (deployment_id,account_ref,schema,policy_sha,enrollment_revision,request_generation,status) VALUES (?,?,?,?,0,0,'prepared')",
        (
            prepared.intent.deployment_id,
            prepared.intent.account_ref,
            LIVE_ACCOUNT_SCHEMA_V2,
            prepared.policy.sha256,
        ),
    )
    with pytest.raises(HttpContractError, match="quiescence_unproven"):
        s.close()
    assert read(prepared)[0].session_state == "running"


def test_lock_contention_subprocess(prepared):
    p, pipe = start(prepared, "hold")
    try:
        assert receive(pipe)[0] == "opened"
        with pytest.raises(HttpContractError, match="store_lock_unavailable"):
            op.open_store(prepared)
        pipe.send("close")
        assert receive(pipe)[0] == "closed"
    finally:
        finish(p, pipe)
    s = op.open_store(prepared)
    s.close()


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("session_started_committed", "blocked"),
        ("session_closed_committed", "clean"),
    ],
)
def test_crash_commit_boundary(prepared, stage, expected):
    p, pipe = start(prepared, "crash-" + stage)
    assert receive(pipe) == ("stage", stage)
    pipe.send("exit")
    finish(p, pipe, 37)
    if expected == "blocked":
        with pytest.raises(HttpContractError, match="restart_uncertain"):
            op.open_store(prepared)
        assert read(prepared)[0].restart_uncertain
    else:
        s = op.open_store(prepared)
        assert s.identity.fence_epoch == 2
        s.close()


def test_dirty_exit_subprocess(prepared):
    p, pipe = start(prepared, "hold")
    assert receive(pipe)[0] == "opened"
    pipe.send("dirty")
    finish(p, pipe, 38)
    with pytest.raises(HttpContractError, match="restart_uncertain"):
        op.open_store(prepared)
    before = read(prepared)
    with pytest.raises(HttpContractError, match="persistent_stop"):
        op.open_store(prepared)
    assert read(prepared) == before


def test_bootstrap_race(authority):
    workers = [start(authority, "bootstrap") for _ in range(2)]
    for _, pipe in workers:
        assert receive(pipe)[0] == "ready"
    for _, pipe in workers:
        pipe.send("go")
    results = [receive(pipe) for _, pipe in workers]
    for p, pipe in workers:
        finish(p, pipe)
    assert sorted(r[0] for r in results) == ["created", "error"]
    p, chain = read(authority)
    assert p.event_count == 1
    assert rt.RuntimeEvent.from_bytes(chain[0]).store_id == next(
        r[1] for r in results if r[0] == "created"
    )


def _fork_probe(prepared):
    # Fork only in a fresh, single-threaded spawn process, not the pytest runner
    # which earlier artificial HTTP tests may have made multi-threaded.
    assert threading.active_count() == 1
    s = op.open_store(prepared)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        passed = 0
        for method in (s.checkpoint_clock, s.close):
            try:
                method()
            except HttpContractError as exc:
                passed += "process_mismatch" in str(exc)
        os.write(write_fd, str(passed).encode())
        os._exit(0)
    os.close(write_fd)
    assert os.read(read_fd, 10) == b"2"
    os.close(read_fd)
    assert os.waitpid(pid, 0)[1] == 0
    with pytest.raises(HttpContractError, match="store_lock_unavailable"):
        op.open_store(prepared)
    s.checkpoint_clock()
    s.close()


def test_fork_child_cannot_use_or_unlock_parent(prepared):
    p, pipe = start(prepared, "fork-probe")
    assert receive(pipe) == ("fork-ok", None)
    finish(p, pipe)


def test_native_local_filesystem_positive(authority):
    fd = os.open(authority.intent.canonical_root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert op._local_filesystem(fd) in ("apfs", "hfs", "ext", "xfs", "btrfs")
    finally:
        os.close(fd)


def test_unknown_filesystem_rejects_before_creation(authority, monkeypatch):
    def reject(fd):
        raise HttpContractError("store_local_filesystem_unverified")

    monkeypatch.setattr(op, "_local_filesystem", reject)
    with pytest.raises(HttpContractError, match="local_filesystem_unverified"):
        op.create_bootstrap(authority)
    assert not Path(authority.intent.db_path).exists()
    assert not Path(authority.intent.lock_path).exists()


def test_no_network_or_business_surface():
    source = inspect.getsource(op)
    for forbidden in (
        "import socket",
        "import requests",
        "os.getenv",
        "os.environ",
        "http_transport",
        "_initialize_schema_v2(",
    ):
        assert forbidden not in source


def test_fullfsync_readback_failure(prepared, monkeypatch):
    if sys.platform != "darwin":
        # Linux has no fullfsync durability requirement; still verify contract.
        assert not prepared.runtime_policy.linux_fullfsync_required
        return
    s = op.open_store(prepared)
    s._resources.connection.execute("PRAGMA fullfsync=OFF")
    with pytest.raises(HttpContractError, match="pragma_fullfsync_invalid"):
        s.close()
    assert read(prepared)[0].session_state == "running"


def test_wrong_thread_does_not_touch_parent(prepared):
    s = op.open_store(prepared)
    results = []

    def wrong():
        for method in (s.checkpoint_clock, s.close):
            try:
                method()
            except HttpContractError as exc:
                results.append(str(exc))

    thread = threading.Thread(target=wrong)
    thread.start()
    thread.join(10)
    assert results == ["store_thread_mismatch"] * 2
    assert s.status == "running"
    s.checkpoint_clock()
    s.close()


@pytest.mark.parametrize("delta", [-5, 5])
def test_operational_exact_drift_boundary(prepared, monkeypatch, delta):
    s = op.open_store(prepared)
    p, _ = read(prepared)
    observation = rt.ClockObservation(
        (
            datetime.fromisoformat(p.last_accepted_utc) + timedelta(seconds=10 + delta)
        ).isoformat(timespec="microseconds"),
        p.last_monotonic_ns + 10_000_000_000,
    )
    monkeypatch.setattr(op, "_capture_clock", lambda: observation)
    assert not s.checkpoint_clock().persistent_stop
    s.close()


@pytest.mark.parametrize("field", ["utc", "monotonic_ns"])
def test_operational_clock_backwards(prepared, monkeypatch, field):
    s = op.open_store(prepared)
    p, _ = read(prepared)
    previous = rt.ClockObservation(p.last_accepted_utc, p.last_monotonic_ns)
    value = (
        (datetime.fromisoformat(previous.utc) - timedelta(microseconds=1)).isoformat(
            timespec="microseconds"
        )
        if field == "utc"
        else previous.monotonic_ns - 1
    )
    monkeypatch.setattr(
        op, "_capture_clock", lambda: replace(previous, **{field: value})
    )
    with pytest.raises(HttpContractError, match="clock_uncertain"):
        s.checkpoint_clock()
    assert read(prepared)[0].persistent_stop


def test_capture_internal_regression_invalidates(prepared, monkeypatch):
    s = op.open_store(prepared)
    samples = iter((10, 9))
    with monkeypatch.context() as patch:
        patch.setattr(op.time, "monotonic_ns", lambda: next(samples))
        with pytest.raises(HttpContractError, match="clock_capture_regressed"):
            s.checkpoint_clock()
    assert s.status == "invalid"
    with pytest.raises(HttpContractError, match="restart_uncertain"):
        op.open_store(prepared)


def test_close_clock_stop_not_clean(prepared, monkeypatch):
    s = op.open_store(prepared)
    monkeypatch.setattr(
        op,
        "_capture_clock",
        lambda: rt.ClockObservation("2000-01-01T00:00:00.000000+00:00", 1),
    )
    with pytest.raises(HttpContractError, match="clock_uncertain"):
        s.close()
    p, records = read(prepared)
    assert p.persistent_stop and p.last_clean_session is None
    assert rt.RuntimeEvent.from_bytes(records[-1]).kind == "runtime_stop_entered"


def test_lock_released_last(prepared, monkeypatch):
    s = op.open_store(prepared)
    lock_fd = s._resources.fds["lock"]
    released = []
    original = op.os.close

    def close_fd(fd):
        released.append(fd)
        return original(fd)

    monkeypatch.setattr(op.os, "close", close_fd)
    s.close()
    assert released[-1] == lock_fd


def test_fullfsync_enforcement_failure_before_mutation(prepared, monkeypatch):
    if sys.platform != "darwin":
        return
    before = read(prepared)
    original = op._check_pragmas

    def fail(c):
        c.execute("PRAGMA fullfsync=OFF")
        return original(c)

    monkeypatch.setattr(op, "_check_pragmas", fail)
    with pytest.raises(HttpContractError, match="pragma_fullfsync_invalid"):
        op.open_store(prepared)
    assert read(prepared) == before


def test_projection_tamper_not_repaired(prepared):
    with closing(sqlite3.connect(prepared.intent.db_path)) as c:
        c.execute("UPDATE runtime_current SET canonical=?", (b"{}",))
        c.commit()
    with pytest.raises(HttpContractError, match="projection_mismatch"):
        op.open_store(prepared)
    with closing(sqlite3.connect(prepared.intent.db_path)) as c:
        assert c.execute("SELECT canonical FROM runtime_current").fetchone()[0] == b"{}"


@pytest.mark.parametrize("role", ["db", "lock", "account_directory", "root"])
def test_pin_identity_mismatch_no_writes(prepared, role):
    physical = prepared.pin.physical
    wrong = replace(physical, **{role: replace(getattr(physical, role), inode=1)})
    a = replace(prepared, pin=replace(prepared.pin, physical=wrong))
    before = read(prepared)
    with pytest.raises(HttpContractError, match="physical_pin_mismatch"):
        op.open_store(a)
    assert read(prepared) == before


def test_persist_failure_rolls_back_checkpoint(prepared, monkeypatch):
    s = op.open_store(prepared)
    before = read(prepared)
    original = op._persist

    def fail(*args):
        original(*args)
        raise sqlite3.OperationalError("artificial post-insert failure")

    monkeypatch.setattr(op, "_persist", fail)
    with pytest.raises(sqlite3.OperationalError):
        s.checkpoint_clock()
    assert read(prepared) == before
    assert s.status == "invalid"


def test_unknown_filesystem_native_probe(authority, monkeypatch):
    class Probe:
        def __call__(self, fd, buffer):
            # ctypes-created buffer remains zero-filled: no local type/flag.
            return 0

    class Lib:
        fstatfs = Probe()

    monkeypatch.setattr(op.ctypes, "CDLL", lambda *args, **kwargs: Lib())
    with pytest.raises(HttpContractError, match="local_filesystem_unverified"):
        op.create_bootstrap(authority)


def test_bootstrap_creation_crash_is_not_auto_finalized(authority):
    # Crash after creating files but before schema is not an empty-store retry.
    lock = Path(authority.intent.lock_path)
    lock.touch(mode=0o600)
    before = lock.stat().st_ino
    with pytest.raises(FileExistsError):
        op.create_bootstrap(authority)
    assert lock.stat().st_ino == before
    assert not Path(authority.intent.db_path).exists()


def test_lock_or_db_not_regular(authority):
    os.mkfifo(authority.intent.lock_path, 0o600)
    with pytest.raises(HttpContractError, match="physical_kind_invalid"):
        op._Resources(authority).acquire()


def test_owner_uid_mismatch(authority, monkeypatch):
    actual = os.geteuid()
    monkeypatch.setattr(op.os, "geteuid", lambda: actual + 1000)
    with pytest.raises(HttpContractError, match="custody_invalid"):
        op.create_bootstrap(authority)


@pytest.mark.parametrize("size", [10, 100, 1000])
def test_replay_performance_samples(prepared, size):
    # Artificial repeated accepted sample; no sleeps or production timing limit.
    s = op.open_store(prepared)
    c, context, records, p = s._begin()
    base_count = len(records)
    previous = p.head_sha
    observation = rt.ClockObservation(p.last_accepted_utc, p.last_monotonic_ns)
    extra = []
    for index in range(size):
        e = rt.RuntimeEvent(
            "clock_observed",
            base_count + index,
            previous,
            context.deployment.deployment_id,
            context.deployment.store_id,
            context.runtime_policy.sha256,
            s.identity.session_id,
            s.identity.fence_epoch,
            observation,
            f"performance-{index}",
            b"{}",
        )
        extra.append(e.canonical)
        previous = e.event_hash
    chain = (*records, *extra)
    new = op._persist(c, context, records, chain)
    c.commit()
    s._head = new.head_sha
    started = time.perf_counter()
    assert rt.replay_runtime(context, chain).head_sha == new.head_sha
    replay = time.perf_counter() - started
    s.close()
    started = time.perf_counter()
    second = op.open_store(prepared)
    startup = time.perf_counter() - started
    second.close()
    print(
        f"\nI1A2B_PERF extra_events={size} replay_seconds={replay:.6f} startup_seconds={startup:.6f}"
    )
