"""I1a-2B physical, exclusive Store v2 runtime. No business/send authority.

Owner snapshots are declarations, not independently authenticated identities.
Private internals are not a same-process hostile-code security boundary.
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import sqlite3
import stat
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from . import live_store as store
from . import live_store_runtime as rt
from .http_contract import HttpContractError
from .live_preflight import LiveAccountPreflightContract


def _require(ok, reason):
    if not ok:
        raise HttpContractError("store_" + reason)


@dataclass(frozen=True)
class OperationalAuthority:
    """Already validated owner snapshots; no alternate paths or clock input."""

    preflight: LiveAccountPreflightContract
    intent: rt.OwnerDeploymentRecord
    policy: store.StorePolicy
    runtime_policy: rt.RuntimePolicy
    pin: rt.OwnerDeploymentRecord | None = None

    def __post_init__(self):
        _require(
            type(self.preflight) is LiveAccountPreflightContract, "preflight_required"
        )
        # Reconstruct to rerun the C1 content contract, without reading credentials.
        replace(self.preflight)
        _require(type(self.intent) is rt.OwnerDeploymentRecord, "intent_required")
        rt.OwnerDeploymentRecord.from_bytes(self.intent.canonical)
        _require(type(self.policy) is store.StorePolicy, "policy_required")
        store.StorePolicy.from_bytes(self.policy.canonical)
        _require(
            type(self.runtime_policy) is rt.RuntimePolicy, "runtime_policy_required"
        )
        rt.RuntimePolicy.from_bytes(self.runtime_policy.canonical)
        i, p = self.intent, self.preflight
        _require(
            i.state == "intent" and i.policy_approval_status == "approved",
            "approved_intent_required",
        )
        _require(
            (
                i.registry_sha,
                i.preflight_sha,
                i.identity_sha,
                i.account_ref,
                i.canonical_root,
                i.db_path,
                i.store_policy_sha,
                i.runtime_policy_sha,
            )
            == (
                p.registry.sha256,
                p.sha256,
                p.identity.sha256,
                p.identity.account_ref,
                p.registry.canonical_store_root,
                p.canonical_ledger_path,
                self.policy.sha256,
                self.runtime_policy.sha256,
            ),
            "authority_binding_mismatch",
        )
        if self.pin is not None:
            _require(type(self.pin) is rt.OwnerDeploymentRecord, "pin_required")
            rt.OwnerDeploymentRecord.from_bytes(self.pin.canonical)
            _require(self.pin.state == "pinned", "pin_required")
            _require(
                replace(
                    self.pin,
                    state="intent",
                    store_uuid=None,
                    physical=None,
                    bootstrap_evidence_sha=None,
                    owner_pin_reference=None,
                )
                == i,
                "pin_intent_mismatch",
            )


@dataclass(frozen=True)
class BootstrapPinCandidate:
    deployment_id: str
    account_ref: str
    store_uuid: str
    physical: rt.PhysicalBinding
    bootstrap_event_sha: str
    owner_intent_sha: str
    store_policy_sha: str
    runtime_policy_sha: str

    @property
    def canonical(self):
        return store.canonical_bytes(
            {
                "schema": "historical-feasibility-bootstrap-pin-candidate-v1",
                **self.__dict__,
                "physical": self.physical.to_dict(),
            }
        )


@dataclass(frozen=True)
class SessionIdentity:
    deployment_id: str
    store_id: str
    account_ref: str
    session_id: str
    fence_epoch: int
    runtime_policy_sha: str


def _capture_clock():
    # Fixed capture order; use the real after sample, never an invented midpoint.
    before = time.monotonic_ns()
    utc = datetime.now(UTC).isoformat(timespec="microseconds")
    after = time.monotonic_ns()
    _require(after >= before, "clock_capture_regressed")
    return rt.ClockObservation(utc, after)


def _stage(name):
    """Private subprocess crash-injection seam; never a public callback."""


class _DarwinStatFS(ctypes.Structure):
    # Darwin sys/mount.h __DARWIN_STRUCT_STATFS64, 64-bit ABI.
    _fields_ = [
        ("bsize", ctypes.c_uint32),
        ("iosize", ctypes.c_int32),
        ("blocks", ctypes.c_uint64),
        ("bfree", ctypes.c_uint64),
        ("bavail", ctypes.c_uint64),
        ("files", ctypes.c_uint64),
        ("ffree", ctypes.c_uint64),
        ("fsid", ctypes.c_int32 * 2),
        ("owner", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("subtype", ctypes.c_uint32),
        ("name", ctypes.c_char * 16),
        ("mount", ctypes.c_char * 1024),
        ("source", ctypes.c_char * 1024),
        ("extended", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 7),
    ]


def _local_filesystem(fd):
    """Positive native FS allowlist, not a proof of underlying hardware custody.

    Reject network/FUSE/overlay/unknown mounts. No subprocess or mount mutation.
    Linux uses the first native long f_type of statfs, in an oversized aligned
    output buffer; only the supported 64-bit ABI is accepted.
    """
    _require(
        sys.platform in ("darwin", "linux") and ctypes.sizeof(ctypes.c_void_p) == 8,
        "filesystem_platform_unsupported",
    )
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.fstatfs
    fn.argtypes = (ctypes.c_int, ctypes.c_void_p)
    fn.restype = ctypes.c_int
    result = _DarwinStatFS() if sys.platform == "darwin" else (ctypes.c_long * 512)()
    _require(fn(fd, ctypes.byref(result)) == 0, "filesystem_probe_failed")
    if sys.platform == "darwin":
        kind = bytes(result.name).decode("ascii", errors="strict")
        _require(
            bool(result.flags & 0x1000) and kind in ("apfs", "hfs"),
            "local_filesystem_unverified",
        )
        return kind
    # EXT2/3/4, XFS, BTRFS: positive local kernel filesystem types.
    kind = {0xEF53: "ext", 0x58465342: "xfs", 0x9123683E: "btrfs"}.get(result[0])
    _require(kind is not None, "local_filesystem_unverified")
    return kind


def _identity(st, directory):
    _require(
        stat.S_ISDIR(st.st_mode) if directory else stat.S_ISREG(st.st_mode),
        "physical_kind_invalid",
    )
    _require(st.st_uid == os.geteuid() and not st.st_mode & 0o022, "custody_invalid")
    if not directory:
        _require(st.st_nlink == 1, "hardlink_forbidden")
    return rt.PhysicalIdentity(
        st.st_dev, st.st_ino, "directory" if directory else "regular_file", st.st_nlink
    )


def _same_identity(left, right):
    # Directory nlink is not an identity: APFS changes it for SQLite's temporary
    # journal entry. Regular-file single-link custody remains mandatory.
    return (left.device, left.inode, left.object_kind) == (
        right.device,
        right.inode,
        right.object_kind,
    ) and (left.object_kind == "directory" or left.link_count == right.link_count == 1)


def _open_root(path):
    """Walk every component by held dirfd, refusing intermediate symlinks too."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open("/", flags)
    try:
        for part in Path(path).parts[1:]:
            prior = os.fstat(fd)
            _require(
                prior.st_uid in (0, os.geteuid()) and not prior.st_mode & 0o022,
                "ancestor_custody_invalid",
            )
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        _identity(os.fstat(fd), True)
        return fd
    except BaseException:
        os.close(fd)
        raise


class _Resources:
    def __init__(self, authority):
        self.authority = authority
        self.pid = os.getpid()
        self.thread = threading.get_ident()
        self.fds = {}
        self.connection = None
        self.locked = False
        self.physical = None

    def acquire(self, *, create=False):
        a = self.authority
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            self.fds["root"] = _open_root(a.intent.canonical_root)
            self.fds["accounts"] = os.open("accounts", flags, dir_fd=self.fds["root"])
            self.fds["account_directory"] = os.open(
                a.intent.account_ref, flags, dir_fd=self.fds["accounts"]
            )
            for name in ("root", "accounts", "account_directory"):
                _identity(os.fstat(self.fds[name]), True)
                _local_filesystem(self.fds[name])
            file_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if create:
                file_flags |= os.O_CREAT | os.O_EXCL
            self.fds["lock"] = os.open(
                rt.LOCK_FILENAME,
                file_flags,
                0o600,
                dir_fd=self.fds["account_directory"],
            )
            _identity(os.fstat(self.fds["lock"]), False)
            try:
                fcntl.flock(self.fds["lock"], fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise HttpContractError("store_lock_unavailable") from exc
            self.locked = True
            self.fds["db"] = os.open(
                Path(a.intent.db_path).name,
                file_flags,
                0o600,
                dir_fd=self.fds["account_directory"],
            )
            self.physical = rt.PhysicalBinding(
                *(
                    _identity(
                        os.fstat(self.fds[name]), name in ("root", "account_directory")
                    )
                    for name in ("root", "account_directory", "db", "lock")
                )
            )
            for name in ("db", "lock"):
                _local_filesystem(self.fds[name])
            if a.pin is not None:
                _require(
                    all(
                        _same_identity(
                            getattr(self.physical, name), getattr(a.pin.physical, name)
                        )
                        for name in ("root", "account_directory", "db", "lock")
                    ),
                    "physical_pin_mismatch",
                )
            self.guard()
            if create:
                for name in ("db", "lock", "account_directory"):
                    os.fsync(self.fds[name])
            self.connection = _connect(a.intent.db_path, bootstrap=create)
            self.guard()
            return self
        except BaseException:
            self.release()
            raise

    def guard(self):
        _require(os.getpid() == self.pid, "process_mismatch")
        _require(threading.get_ident() == self.thread, "thread_mismatch")
        _require(self.locked, "lock_not_held")
        # Re-open canonical ancestry rather than treating resolve() as authority.
        probe = _open_root(self.authority.intent.canonical_root)
        try:
            _require(
                os.fstat(probe)[:2] == os.fstat(self.fds["root"])[:2], "root_replaced"
            )
        finally:
            os.close(probe)
        paths = {
            "root": self.authority.intent.canonical_root,
            "accounts": str(Path(self.authority.intent.canonical_root) / "accounts"),
            "account_directory": str(Path(self.authority.intent.db_path).parent),
            "db": self.authority.intent.db_path,
            "lock": self.authority.intent.lock_path,
        }
        for name, path in paths.items():
            directory = name in ("root", "accounts", "account_directory")
            current = _identity(os.lstat(path), directory)
            held = _identity(os.fstat(self.fds[name]), directory)
            _require(current == held, "physical_path_replaced")
            if name != "accounts":
                _require(
                    _same_identity(held, getattr(self.physical, name)),
                    "physical_pin_changed",
                )
        fcntl.flock(self.fds["lock"], fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self):
        # Never close/unlock inherited parent resources through a child API.
        _require(os.getpid() == self.pid, "process_mismatch")
        _require(threading.get_ident() == self.thread, "thread_mismatch")
        try:
            if self.connection is not None:
                self.connection.close()
        finally:
            self.connection = None
            try:
                for name in tuple(self.fds):
                    if name != "lock":
                        os.close(self.fds.pop(name))
            finally:
                if "lock" in self.fds:
                    fd = self.fds.pop("lock")
                    try:
                        if self.locked:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)
                self.locked = False


_PRAGMAS = {
    "foreign_keys": 1,
    "recursive_triggers": 1,
    "synchronous": 3,
    "busy_timeout": 0,
    "locking_mode": "normal",
    "trusted_schema": 0,
    "ignore_check_constraints": 0,
}


def _check_pragmas(c):
    version = tuple(
        int(n) for n in c.execute("SELECT sqlite_version()").fetchone()[0].split(".")
    )
    _require(version >= (3, 31, 0), "sqlite_version_unsupported")
    expected = {**_PRAGMAS, "journal_mode": "delete"}
    if sys.platform == "darwin":
        expected["fullfsync"] = 1
    for key, value in expected.items():
        row = c.execute(f"PRAGMA {key}").fetchone()
        _require(row is not None and row[0] == value, "pragma_" + key + "_invalid")


def _connect(path, *, bootstrap):
    c = sqlite3.connect(
        Path(path).as_uri() + "?mode=rw", uri=True, timeout=0, isolation_level=None
    )
    try:
        _require(
            sqlite3.sqlite_version_info >= (3, 31, 0), "sqlite_version_unsupported"
        )
        if bootstrap:
            c.execute("PRAGMA journal_mode=DELETE")
        else:
            _require(
                c.execute("PRAGMA journal_mode").fetchone()[0] == "delete",
                "journal_mode_invalid",
            )
        for key, value in _PRAGMAS.items():
            c.execute(f"PRAGMA {key}={value}")
        if sys.platform == "darwin":
            c.execute("PRAGMA fullfsync=ON")
        _check_pragmas(c)
        return c
    except BaseException:
        c.close()
        raise


def _authority(value, *, pin_required):
    _require(type(value) is OperationalAuthority, "authority_required")
    replace(value)
    _require(not pin_required or value.pin is not None, "owner_pin_required")
    _require(
        sys.platform in value.runtime_policy.supported_platforms, "platform_unsupported"
    )
    return value


def _read_context(c, a):
    p = rt.validate_store_v2_contents(c)
    if c.execute(
        "SELECT 1 FROM operations WHERE kind IN ('i1b-account-open-v1','i1b-plan-enroll-v1') LIMIT 1"
    ).fetchone():
        from .live_store_transactions import _reconcile

        _reconcile(c)
    d = store._rows(c, "deployments")[0]
    deployment = store.DeploymentRecord(
        **{
            name: d["policy_sha"] if name == "store_policy_sha" else d[name]
            for name in store.DeploymentRecord.__dataclass_fields__
        }
    )
    owners = tuple(
        rt.OwnerDeploymentRecord.from_bytes(r["canonical"])
        for r in store._rows(c, "owner_deployment_records")
    )
    _require(a.intent in owners, "owner_intent_mismatch")
    _require(deployment.store_policy_sha == a.policy.sha256, "policy_mismatch")
    pins = tuple(o for o in owners if o.state == "pinned")
    _require(not pins or pins == (a.pin,), "owner_pin_mismatch")
    context = rt.RuntimeContext(deployment, a.runtime_policy, a.intent, pins)
    if a.pin:
        _require(deployment.store_id == a.pin.store_uuid, "store_uuid_mismatch")
    records = tuple(
        r[0]
        for r in c.execute("SELECT canonical FROM runtime_events ORDER BY sequence")
    )
    return context, records, p


def _integrity(c):
    _check_pragmas(c)
    _require(
        c.execute("PRAGMA quick_check").fetchall() == [("ok",)],
        "sqlite_integrity_invalid",
    )
    _require(
        not c.execute("PRAGMA foreign_key_check").fetchall(), "foreign_key_invalid"
    )


def _persist(c, context, old, records):
    """Caller holds BEGIN IMMEDIATE and the physical lock. No business writes."""
    _require(c.in_transaction, "transaction_required")
    _require(records[: len(old)] == old, "runtime_prefix_changed")
    d = context.deployment.deployment_id
    for data in records[len(old) :]:
        e = rt.RuntimeEvent.from_bytes(data)
        c.execute(
            "INSERT INTO runtime_events VALUES (?,?,?,?,?)",
            (d, e.sequence, e.event_hash, e.previous_hash, data),
        )
    p = rt.replay_runtime(context, records)
    c.execute(
        "INSERT OR REPLACE INTO runtime_current VALUES (?,?,?,?,?)",
        (d, p.event_count, p.event_count - 1, p.head_sha, p.canonical),
    )
    for s in p.sessions:
        c.execute(
            "INSERT OR REPLACE INTO runtime_sessions VALUES (?,?,?,?,?,?)",
            (d, s.session_id, s.fence_epoch, s.start_event, s.close_event, s.canonical),
        )
    rt.validate_store_v2_contents(c)
    return p


def _append(context, records, kind, observation, payload, *, session=None, epoch=None):
    p = rt.replay_runtime(context, records)
    return rt.append_runtime_event(
        context,
        records,
        kind=kind,
        observation=observation,
        transition_id=str(uuid4()),
        session_id=p.current_session_id if session is None else session,
        fence_epoch=p.fence_epoch if epoch is None else epoch,
        payload=payload,
    )


def _bootstrap_schema(c, context, policy, documents, records):
    """Production bootstrap in caller-owned transaction; no artificial initializer."""
    _require(c.in_transaction, "transaction_required")
    _require(
        not c.execute("SELECT name FROM sqlite_master").fetchall(), "existing_schema"
    )
    for sql in rt._DDL_V2:
        c.execute(sql)
    d = context.deployment
    c.execute(
        "INSERT INTO policies VALUES (?,?,?,?,?,?,?)",
        (
            policy.sha256,
            store.POLICY_SCHEMA,
            policy.canonical,
            *policy.__dict__.values(),
        ),
    )
    c.execute(
        "INSERT INTO deployments VALUES (1,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            d.deployment_id,
            d.store_id,
            d.account_ref,
            policy.sha256,
            d.owner_registry_sha,
            d.preflight_sha,
            "owner_registry",
            store.LIVE_ACCOUNT_REGISTRY_SCHEMA,
            "preflight",
            store.LIVE_ACCOUNT_PREFLIGHT_SCHEMA,
            d.created_at,
            "bootstrap_pending",
            rt._deployment_bytes(d),
        ),
    )
    for doc in documents:
        c.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
            (
                d.deployment_id,
                doc.sha256,
                doc.kind,
                doc.schema,
                doc.content,
                doc.source_identity,
                doc.source_reference,
                doc.registered_at,
            ),
        )
    c.execute(
        "INSERT INTO store_metadata VALUES (1,?,?,?)",
        (
            rt.STORE_SCHEMA_V2,
            d.deployment_id,
            store.canonical_bytes(
                {"schema": rt.STORE_SCHEMA_V2, "deployment_id": d.deployment_id}
            ),
        ),
    )
    rp, intent = context.runtime_policy, context.owner_intent
    c.execute(
        "INSERT INTO runtime_policies VALUES (?,?,?)",
        (rp.sha256, rt.RUNTIME_POLICY_SCHEMA, rp.canonical),
    )
    c.execute(
        "INSERT INTO owner_deployment_records VALUES (?,?,?,?)",
        (d.deployment_id, intent.sha256, intent.state, intent.canonical),
    )
    c.execute(
        "INSERT INTO runtime_bindings VALUES (?,?,?)",
        (d.deployment_id, rp.sha256, intent.sha256),
    )
    _persist(c, context, (), records)


def create_bootstrap(authority):
    a = _authority(authority, pin_required=False)
    _require(a.pin is None, "bootstrap_intent_only")
    r = _Resources(a).acquire(create=True)
    c = r.connection
    try:
        r.guard()
        c.execute("BEGIN IMMEDIATE")
        observation = _capture_clock()
        d = store.DeploymentRecord(
            str(uuid4()),
            a.intent.deployment_id,
            a.intent.account_ref,
            a.intent.registry_sha,
            a.intent.preflight_sha,
            a.policy.sha256,
            observation.utc,
        )
        context = rt.RuntimeContext(d, a.runtime_policy, a.intent)
        records = _append(
            context,
            (),
            "bootstrap_created",
            observation,
            {"intent_sha": a.intent.sha256, "physical": r.physical.to_dict()},
        )
        documents = []
        for kind, raw in (
            ("owner_registry", a.preflight.registry.to_dict()),
            ("preflight", a.preflight.to_dict()),
        ):
            content = store.canonical_bytes(raw)
            documents.append(
                store.CanonicalDocument(
                    kind,
                    raw["schema"],
                    content,
                    store.digest_bytes(content),
                    "owner-snapshot",
                    a.intent.policy_decision_reference,
                    observation.utc,
                )
            )
        _bootstrap_schema(c, context, a.policy, tuple(documents), records)
        r.guard()
        c.commit()
        _stage("bootstrap_committed")
        return BootstrapPinCandidate(
            d.deployment_id,
            d.account_ref,
            d.store_id,
            r.physical,
            rt.replay_runtime(context, records).head_sha,
            a.intent.sha256,
            a.policy.sha256,
            a.runtime_policy.sha256,
        )
    finally:
        r.release()


def finalize_bootstrap(authority):
    a = _authority(authority, pin_required=True)
    r = _Resources(a).acquire()
    c = r.connection
    try:
        r.guard()
        c.execute("BEGIN IMMEDIATE")
        _integrity(c)
        context, records, p = _read_context(c, a)
        _require(
            p.lifecycle == "bootstrap_pending" and not p.persistent_stop,
            "bootstrap_not_pending",
        )
        pin = a.pin
        context = replace(context, owner_pins=(pin,))
        rt.replay_runtime(context, records)
        observation = _capture_clock()
        c.execute(
            "INSERT INTO owner_deployment_records VALUES (?,?,?,?)",
            (pin.deployment_id, pin.sha256, pin.state, pin.canonical),
        )
        result = _append(
            context,
            records,
            "bootstrap_finalized",
            observation,
            {
                "owner_pin_sha": pin.sha256,
                "owner_pin_reference": pin.owner_pin_reference,
                "bootstrap_head": pin.bootstrap_evidence_sha,
                "lifecycle": "prepared",
            },
        )
        p = _persist(c, context, records, result)
        r.guard()
        c.commit()
        return p
    finally:
        r.release()


def _startup_stop(context, records, p, observation, reason):
    return _append(
        context,
        records,
        "runtime_stop_entered",
        observation,
        {
            "reason": reason,
            "clock_reasons": ["utc_backward"]
            if observation.utc < p.last_accepted_utc
            else [],
            "evidence_reference": "operational-startup-guard",
        },
    )


def open_store(authority):
    a = _authority(authority, pin_required=True)
    r = _Resources(a).acquire()
    c = r.connection
    try:
        r.guard()
        c.execute("BEGIN IMMEDIATE")
        _integrity(c)
        context, records, p = _read_context(c, a)
        _require(not p.persistent_stop, "persistent_stop")
        assessment = rt.assess_startup(context, records)
        _require(
            assessment.classification != "bootstrap_incomplete",
            "explicit_finalization_required",
        )
        observation = _capture_clock()
        reason = (
            "restart_uncertain"
            if p.session_state == "running"
            else ("clock_uncertain" if observation.utc < p.last_accepted_utc else None)
        )
        if reason:
            result = _startup_stop(context, records, p, observation, reason)
            _persist(c, context, records, result)
            r.guard()
            c.commit()
            raise HttpContractError("store_" + reason)
        _require(
            assessment.classification == "new_session_candidate", "startup_blocked"
        )
        result = _append(
            context,
            records,
            "session_started",
            observation,
            {"owner_pin_sha": a.pin.sha256},
            session=str(uuid4()),
            epoch=p.fence_epoch + 1,
        )
        p = _persist(c, context, records, result)
        r.guard()
        c.commit()
        _stage("session_started_committed")
        return StoreSession(_HANDLE_KEY, r, context, p)
    except BaseException:
        r.release()
        raise


_HANDLE_KEY = object()


class StoreSession:
    """Exclusive runtime-only handle. Explicit close; no destructor/auto recovery."""

    def __init__(self, key, resources, context, projection):
        _require(key is _HANDLE_KEY, "open_store_required")
        self._resources = resources
        self._context = context
        self._identity = SessionIdentity(
            context.deployment.deployment_id,
            context.deployment.store_id,
            context.deployment.account_ref,
            projection.current_session_id,
            projection.fence_epoch,
            context.runtime_policy.sha256,
        )
        self._head = projection.head_sha
        self._status = "running"

    @property
    def identity(self):
        return self._identity

    @property
    def status(self):
        return self._status

    def _begin(self):
        r = self._resources
        r.guard()
        _require(self._status == "running", "session_not_usable")
        c = r.connection
        _require(not c.in_transaction, "unexpected_transaction")
        c.execute("BEGIN IMMEDIATE")
        _integrity(c)
        context, records, p = _read_context(c, r.authority)
        _require(
            (p.current_session_id, p.fence_epoch, p.head_sha)
            == (self.identity.session_id, self.identity.fence_epoch, self._head)
            and p.session_state == "running"
            and not p.persistent_stop,
            "session_fence_mismatch",
        )
        return c, context, records, p

    def _invalidate(self):
        self._status = "invalid"
        self._resources.release()

    def checkpoint_clock(self):
        _require(os.getpid() == self._resources.pid, "process_mismatch")
        _require(threading.get_ident() == self._resources.thread, "thread_mismatch")
        try:
            c, context, records, _ = self._begin()
            result = rt.record_clock_observation(
                context, records, _capture_clock(), str(uuid4())
            )
            p = _persist(c, context, records, result)
            self._resources.guard()
            c.commit()
            self._head = p.head_sha
            _require(not p.persistent_stop, "clock_uncertain")
            return p
        except BaseException:
            self._invalidate()
            raise

    def close(self):
        _require(os.getpid() == self._resources.pid, "process_mismatch")
        _require(threading.get_ident() == self._resources.thread, "thread_mismatch")
        if self._status == "closed":
            return
        _require(self._status == "running", "session_not_usable")
        try:
            c, context, records, p = self._begin()
            self._status = "closing"
            observed = _capture_clock()
            checked = rt.record_clock_observation(
                context, records, observed, str(uuid4())
            )
            p = _persist(c, context, records, checked)
            if p.persistent_stop:
                self._resources.guard()
                c.commit()
                raise HttpContractError("store_clock_uncertain")
            # Preserve the empty v1 profile byte-for-byte. Nonempty evidence
            # requires I1a-3's separate global quiescence proof/profile.
            tables = (
                "accounts",
                "plans",
                "journals",
                "events",
                "operations",
                "receipts",
                "control_state",
                "journal_heads",
            )
            summary = {
                t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables
            }
            snapshot = {
                "schema": "historical-feasibility-runtime-clean-validation-v1",
                "store_schema": rt.STORE_SCHEMA_V2,
                **self.identity.__dict__,
                "runtime_head": p.head_sha,
                "owner_pin_sha": p.owner_pin_sha,
                "physical_sha": p.physical_sha,
                "journal_heads": [],
                "business_row_counts": summary,
            }
            if any(summary.values()):
                from .live_store_transactions import _clean_snapshot

                snapshot = _clean_snapshot(c, self.identity, p)
            result = _append(
                context,
                checked,
                "session_closed",
                observed,
                {
                    "predecessor": p.head_sha,
                    "quiescent": True,
                    "validation_sha": store.digest_bytes(
                        store.canonical_bytes(snapshot)
                    ),
                },
            )
            _persist(c, context, checked, result)
            self._resources.guard()
            c.commit()
            _stage("session_closed_committed")
            self._status = "closed"
            self._resources.release()
        except BaseException:
            self._invalidate()
            raise
