"""Stage-specific diagnostic checks; never an execution gate or authorization."""

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from delayed_replay import june_clearing as clear
from delayed_replay import june_trial as june
from delayed_replay.input_artifacts import InputPacket
from delayed_replay.serialization import JsonObject, digest, parse_time, time_text

from .order_audit import _price_evidence
from .preflight_inputs import (
    PreflightEvidence,
    finite_prefixes,
    history_snapshot,
    receipt_snapshot,
    validate_captures,
)
from .reader import ObservationError, read_account

STAGES = ("acquisition", "build_inputs", "clearing", "resume_account")
STATUSES = ("pass", "blocked", "unverified", "not_applicable", "error")


class CheckIssue(ValueError):
    def __init__(self, status, reason, details=None):
        self.status, self.reason, self.details = status, reason, details or {}


@dataclass(frozen=True)
class PreflightCheckResult:
    stage: str
    check_id: str
    status: str
    reason: str
    checked_at: str
    required: bool
    evidence_refs: tuple[str, ...]
    next_action: str
    details: JsonObject

    def __post_init__(self):
        if (
            self.stage not in STAGES
            or self.status not in STATUSES
            or type(self.required) is not bool
        ):
            raise ValueError("invalid_preflight_check")
        parse_time(self.checked_at)

    def to_dict(self):
        return dict(
            stage=self.stage,
            check_id=self.check_id,
            status=self.status,
            reason=self.reason,
            checked_at=self.checked_at,
            required=self.required,
            evidence_refs=list(self.evidence_refs),
            next_action=self.next_action,
            details=self.details.to_dict(),
        )


@dataclass(frozen=True)
class StageReadinessResult:
    stage: str
    checks: tuple[PreflightCheckResult, ...]

    def __post_init__(self):
        if (
            self.stage not in STAGES
            or not self.checks
            or any(c.stage != self.stage for c in self.checks)
            or len({c.check_id for c in self.checks}) != len(self.checks)
            or not any(c.required for c in self.checks)
        ):
            raise ValueError("invalid_preflight_stage")

    def to_dict(self):
        required = [c for c in self.checks if c.required]
        ready = all(c.status == "pass" for c in required)
        status = next(
            (
                s
                for s in ("error", "blocked", "unverified", "not_applicable")
                if any(c.status == s for c in required)
            ),
            "ready",
        )
        return dict(
            stage=self.stage,
            status=status,
            **{"ready_for_" + self.stage: ready},
            reasons=[
                c.reason
                for c in self.checks
                if c.status not in ("pass", "not_applicable")
            ],
            checks=[c.to_dict() for c in self.checks],
        )


@dataclass(frozen=True)
class PreflightBundle:
    payload: JsonObject
    files: PreflightEvidence
    input_root: Path
    other_input_root: Path


class ReadOnlyPreflightInspector:
    def __init__(self, *, clock=None, key_present=None):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.key_present = key_present or (
            lambda: bool(os.environ.get("JQUANTS_API_KEY", "").strip())
        )

    def inspect(
        self,
        trial_root,
        may_root,
        *,
        stage="all",
        acquisition_authorization="owner_approved_acquisition.json",
        clearing_authorization="owner_approved_clearing.json",
        account=None,
    ):
        if stage not in (*STAGES, "all"):
            raise ValueError("unknown_preflight_stage")
        now = self.clock()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError("aware_inspection_clock_required")
        now = now.astimezone(UTC)
        stamp = time_text(now)
        root, may = Path(trial_root).absolute(), Path(may_root).absolute()
        files = PreflightEvidence(root, may)
        values = {}
        outcomes = {}
        stages = []

        def load(name):
            if root not in (root / name).resolve().parents:
                raise ObservationError("input_must_belong_to_trial")
            return files.json(root / name).to_dict()

        def run(which, check_id, fn, deps=(), required=True):
            if check_id not in outcomes:
                details = {}
                refs = ()
                if any(outcomes.get(dep, ("unverified",))[0] != "pass" for dep in deps):
                    status, reason = "unverified", check_id + "_prerequisite_unverified"
                    details = {"dependencies": list(deps)}
                else:
                    try:
                        value = fn()
                        values[check_id] = value
                        status, reason = "pass", check_id + "_verified"
                        if isinstance(value, dict):
                            details = value
                    except CheckIssue as e:
                        status, reason, details = e.status, e.reason, e.details
                    except (OSError, FileNotFoundError):
                        status, reason = "unverified", check_id + "_missing_evidence"
                    except ObservationError as e:
                        if str(e) == "missing_input":
                            status, reason = (
                                "unverified",
                                check_id + "_missing_evidence",
                            )
                        elif str(e) in (
                            "stopped_consistent_non_wal_snapshot_required",
                            "unsupported_journal_mode",
                        ):
                            status, reason = (
                                "blocked",
                                "stopped_consistent_non_wal_snapshot_required",
                            )
                        else:
                            status, reason = (
                                "error",
                                check_id + "_integrity_or_schema_error",
                            )
                    except (
                        ValueError,
                        TypeError,
                        KeyError,
                        AttributeError,
                        IndexError,
                        StopIteration,
                        OverflowError,
                    ):
                        status, reason = (
                            "error",
                            check_id + "_integrity_or_schema_error",
                        )
                # References are hashes only; never paths, grants or credential values.
                refs = tuple(files.hashes)
                outcomes[check_id] = (status, reason, details, refs)
            status, reason, details, refs = outcomes[check_id]
            return PreflightCheckResult(
                which,
                check_id,
                status,
                reason,
                stamp,
                required,
                refs,
                {
                    "pass": "no_action_by_inspector",
                    "blocked": "resolve_condition_then_reinspect",
                    "unverified": "provide_required_evidence_then_reinspect",
                    "error": "investigate_integrity_without_repair",
                    "not_applicable": "no_action_for_this_stage",
                }[status],
                JsonObject.from_value(details),
            )

        def plan():
            return june.JunePlan(files.json(root / "plan.json"))

        def implementation():
            p = values["plan"].payload.to_dict()
            if p["implementation_hash"] != june.june_implementation_hash():
                raise ObservationError("implementation_changed")
            return dict(
                plan_hash=values["plan"].sha256,
                implementation_hash=p["implementation_hash"],
                settings_hash=p["settings_hash"],
            )

        def history():
            return history_snapshot(may, values["plan"], files)

        def lot():
            p = values["plan"].payload.to_dict()
            review = files.artifact(root, p["lot_review_hash"])
            june.require_lot(review, may, values["history"][0])
            return dict(
                lot_review_hash=p["lot_review_hash"],
                lot_size=100,
                limitation="retrospective_document_review_not_contemporaneous",
            )

        def receipt():
            return receipt_snapshot(root, values["plan"], files)

        def time_window():
            p = values["plan"].payload.to_dict()
            if now < parse_time(p["not_before"]):
                raise CheckIssue(
                    "blocked",
                    "before_acquisition_not_before",
                    dict(not_before=p["not_before"]),
                )
            if (
                date(2026, 7, 1) > (now - timedelta(weeks=12)).date()
                or date(2026, 1, 1) < (now - timedelta(days=730)).date()
            ):
                raise CheckIssue("blocked", "outside_existing_acquisition_date_window")
            return dict(
                not_before=p["not_before"], official_availability="not_proven_by_clock"
            )

        def acquisition_grant():
            a = load(acquisition_authorization)
            if (
                set(a)
                != {
                    "schema",
                    "plan_hash",
                    "status",
                    "approval_reference",
                    "permission",
                    "execution_permission",
                }
                or a["schema"] != "june-acquisition-authorization-v1"
                or a["plan_hash"] != values["plan"].sha256
            ):
                raise ObservationError("acquisition_grant_binding")
            if (
                a["permission"] != "acquire_only"
                or a["execution_permission"] is not False
                or a["status"] != "approved_for_acquisition"
            ):
                raise CheckIssue("blocked", "separate_acquisition_permission_required")
            june.require_text(a["approval_reference"])
            if a["approval_reference"].startswith("local-owner-approval:"):
                sha = a["approval_reference"].split(":", 1)[1]
                june.require_hash(sha)
                record = files.json(root / "owner_approval_record.json", sha).to_dict()
                p = values["plan"].payload.to_dict()
                if (
                    record.get("schema") != "june-local-owner-approval-record-v1"
                    or record["acquisition_plan_hash"] != values["plan"].sha256
                    or record["implementation_hash"] != p["implementation_hash"]
                    or record["settings_hash"] != p["settings_hash"]
                    or record[
                        "acquisition_approved_subject_to_not_before_and_official_window"
                    ]
                    is not True
                ):
                    raise ObservationError("owner_approval_record_binding")
            return dict(
                authorization_hash=digest(a),
                permission="acquire_only",
                not_a_clearing_grant=True,
            )

        def acquisition_provenance():
            if (
                values["plan"].payload.to_dict()["parent"]["provenance"]
                != "saved_jquants"
            ):
                raise CheckIssue("blocked", "artificial_live_acquisition_forbidden")
            return dict(provenance="saved_jquants")

        def budget_time():
            r = values["receipt"]
            stats = dict(r.statistics)
            if any(parse_time(e["at"]) > now for e in r.events()):
                raise CheckIssue("error", "inspection_clock_precedes_receipt")
            if stats["deadline"] and now >= parse_time(stats["deadline"]):
                raise CheckIssue("blocked", "acquisition_deadline_exhausted", stats)
            if any(e["kind"] == "stopped" for e in r.events()):
                raise CheckIssue("blocked", "acquisition_previously_stopped", stats)
            return dict(
                **stats,
                budget_state="not_started"
                if stats["started_at"] is None
                else "remaining",
                head=r.head,
            )

        def attempt_budget():
            stats = values["receipt"].statistics
            if stats["attempts"] >= 20:
                raise CheckIssue(
                    "blocked",
                    "acquisition_attempt_limit",
                    dict(attempts=stats["attempts"], max_attempts=20),
                )
            return dict(
                attempts=stats["attempts"], remaining_attempts=20 - stats["attempts"]
            )

        def key_presence():
            present = self.key_present()
            if type(present) is not bool:
                raise ValueError("boolean_key_presence_required")
            if not present:
                raise CheckIssue("blocked", "api_key_absent_in_inspector_process")
            return dict(present=True, authentication="not_proven_by_presence")

        def official_range():
            recorded = load("authorized_local_preflight.json")
            if (
                recorded.get("schema") != "june-authorized-local-preflight-v1"
                or recorded["plan_hash"] != values["plan"].sha256
            ):
                raise ObservationError("official_review_record_binding")
            reviewed = parse_time(recorded["recorded_at"])
            if reviewed > now:
                raise ObservationError("future_review_record")
            # Current plan defines no reusable verification-record/freshness contract.
            # Never turn an old Boolean/report into a current official-site check.
            raise CheckIssue(
                "unverified",
                "official_window_requires_current_review",
                dict(
                    saved_record_hash=digest(recorded),
                    recorded_at=time_text(reviewed),
                    freshness_contract="not_defined_by_existing_plan",
                ),
            )

        def authentication():
            responses = [
                e
                for e in values["receipt"].events()
                if e["kind"] == "response" and e["data"]["status"] == 200
            ]
            if not responses:
                raise CheckIssue("unverified", "authentication_not_observed_no_http")
            raise CheckIssue(
                "unverified",
                "current_authentication_not_retested",
                dict(
                    historical_http_200_at=responses[-1]["at"],
                    provenance=values["plan"].payload.to_dict()["parent"]["provenance"],
                ),
            )

        def captures():
            r = values["receipt"]
            if {
                digest(e["data"]["capture"]["query"])
                for e in r.events()
                if e["kind"] == "capture"
            } != {digest(q) for q in june.queries()}:
                raise CheckIssue("unverified", "acquisition_responses_incomplete")
            return june.receipt_captures(r)

        def capture_contract():
            return validate_captures(values["receipt"], values["history"])

        def input_manifest():
            m = load("input_manifest.json")
            p = values["plan"].payload.to_dict()
            captured, parts, comparison, reported = values["capture_contract"]
            if (
                m.get("schema") != "june-input-manifest-v1"
                or m["plan_hash"] != values["plan"].sha256
                or m["scope"] != june.SCOPE
                or m["settings_hash"] != p["settings_hash"]
                or m["implementation_hash"] != p["implementation_hash"]
                or m["history_packets"] != p["parent"]["packets"]
                or m["parts"] != [list(x) for x in parts]
                or m["history_comparison"] != comparison
                or m["captures"] != [digest(c) for c in captured]
                or m["lot_review_hash"] != p["lot_review_hash"]
                or m["provenance"] != p["parent"]["provenance"]
                or m["input_ready"] is not True
                or m["executable"] is not False
                or m["formal_oos"] is not False
                or m["clearing"] != "not_executed"
                or m["causal_features"] != "finite_session_prefixes_only"
                or len(m["run_packets"]) != 2
            ):
                raise ObservationError("input_manifest_binding")
            for c in captured:
                if files.artifact(root, digest(c)) != c:
                    raise ObservationError("input_capture_changed")
            for sha, part in zip(m["run_packets"], parts, strict=True):
                june.require_hash(sha)
                packet = InputPacket.from_payload(
                    files.json(root / "inputs" / (sha + ".json"), sha)
                )
                seen = []
                if len(packet.market.snapshots) != 1 or packet.market.open_snapshots:
                    raise ObservationError("input_packet_shape")
                snapshot = packet.market.snapshots[0]
                if (
                    snapshot.provider != "jquants"
                    or snapshot.provider_price_basis
                    != june.provider_price_basis("jquants")
                    or snapshot.source_artifact_sha256 != digest(captured[3])
                    or time_text(snapshot.fetched_at) != captured[3]["fetched_at"]
                    or snapshot.first_observed_at != snapshot.fetched_at
                ):
                    raise ObservationError("input_snapshot_lineage")
                for bar in snapshot.observations:
                    day = bar.session_date.isoformat()
                    seen.append(day)
                    vals = list(
                        map(june.numeric, bar.raw_ohlcv + bar.adjusted_ohlcv)
                    ) + [june.numeric(bar.adjustment_factor)]
                    if (
                        bar.symbol != "46890"
                        or day not in reported
                        or vals != reported[day]["values"]
                    ):
                        raise ObservationError("input_packet_values")
                if seen != list(part):
                    raise ObservationError("input_packet_sessions")
            return m

        def clearing_plan():
            p = clear.JuneClearingPlan(files.json(root / "clearing_plan.json"))
            expected = values["history"][0].payload.to_dict()
            m = values["input_manifest"]
            expected.update(
                schema="june-clearing-plan-v1",
                scope=clear.SCOPE,
                model_hash=june.june_implementation_hash(),
                source_identity="june-input-manifest:" + digest(m),
                packets=m["run_packets"],
                history_packets=m["history_packets"],
                captures=dict(
                    calendar=m["captures"][0],
                    master=m["captures"][1],
                    daily_source=m["captures"][3],
                    daily_capture=m["captures"][3],
                ),
                references=dict(
                    lot=dict(review_hash=m["lot_review_hash"]),
                    price="jquants_reported_separate_adjusted_not_tick_execution",
                    halt="unknown_no_independent_halt_feed",
                    external_price=None,
                ),
            )
            if p.payload.to_dict() != expected:
                raise ObservationError("clearing_plan_binding")
            return p

        def clearing_grant():
            obj = JsonObject.from_value(load(clearing_authorization))
            a = clear.JuneClearingAuthorization(obj)
            try:
                a.require(values["clearing_plan"])
            except ValueError:
                raise CheckIssue(
                    "blocked", "separate_clearing_permission_required"
                ) from None
            if obj.to_dict()["acquisition_plan_hash"] != values["plan"].sha256:
                raise ObservationError("clearing_acquisition_binding")
            return dict(authorization_hash=obj.sha256)

        def account_state():
            if account is None:
                raise CheckIssue("unverified", "saved_account_not_specified")
            path = root / account
            if root not in path.resolve().parents:
                raise ObservationError("account_outside_trial")
            with read_account(path, files) as stored:
                state = stored.current_state.to_dict()
                identity = state["identity"]
                if (
                    state["schema"] != "limited-proxy-state-v1"
                    or identity != stored.initial_state.to_dict()["identity"]
                    or digest(identity) != stored.identity.config_hash
                    or identity["plan"] != values["clearing_plan"].payload.to_dict()
                    or identity["plan_hash"] != values["clearing_plan"].sha256
                    or identity["june_clearing_authorization"]
                    != load(clearing_authorization)
                ):
                    raise ObservationError("account_plan_identity")
                known = set(
                    values["input_manifest"]["history_packets"]
                    + values["input_manifest"]["run_packets"]
                )
                accepted = state["accepted_packets"]
                if (
                    type(accepted) is not list
                    or len(set(accepted)) != len(accepted)
                    or not set(accepted) <= known
                    or any(
                        v["packet"] not in accepted for v in state["inputs"].values()
                    )
                ):
                    raise ObservationError("account_accepted_input_identity")
                expected_keys = set()
                for sha in accepted:
                    june.require_hash(sha)
                    base = (
                        may
                        if sha in values["input_manifest"]["history_packets"]
                        else root
                    )
                    packet = InputPacket.from_payload(
                        files.json(base / "inputs" / (sha + ".json"), sha)
                    )
                    expected_keys.update(
                        b.symbol + "|" + b.session_date.isoformat()
                        for snap in packet.market.snapshots
                        for b in snap.observations
                    )
                if set(state["inputs"]) != expected_keys:
                    raise ObservationError("accepted_packet_coverage")
                days = identity["run_sessions"]
                if (
                    days != sum(values["input_manifest"]["parts"], [])
                    or type(state["index"]) is not int
                    or not 0 <= state["index"] <= len(days)
                    or state["phase"]
                    not in ("select", "resolve", "mark", "decide", "finish")
                    or state["input_head"] not in state["versions"]
                    or any(digest(v) != h for h, v in state["versions"].items())
                ):
                    raise ObservationError("account_cursor_or_input_chain")
                seen = set()
                head = state["input_head"]
                while head is not None:
                    if head in seen or head not in state["versions"]:
                        raise ObservationError("input_version_cycle_or_missing")
                    seen.add(head)
                    head = state["versions"][head]["parent"]
                if seen != set(state["versions"]):
                    raise ObservationError("orphan_input_version")
                cache = {}
                for item in state["inputs"].values():
                    base = (
                        may
                        if item["packet"] in values["input_manifest"]["history_packets"]
                        else root
                    )
                    _, _, missing = _price_evidence(
                        base,
                        state,
                        item["row"]["symbol"],
                        item["row"]["session"],
                        files,
                        cache,
                    )
                    if missing:
                        raise CheckIssue(
                            "unverified", "accepted_input_price_evidence_missing"
                        )
                for event in stored.events:
                    payload = event.command.payload.to_dict()
                    if payload.get("schema") != "limited-proxy-event-v1" or payload.get(
                        "action"
                    ) not in ("phase", "extension"):
                        raise ObservationError("unknown_account_event")
                    if payload["action"] == "extension":
                        d = payload["data"]
                        if (
                            d["parent"] not in state["versions"]
                            or d["packet"] not in identity["catalog"]
                            or digest(d["rows"]) != identity["catalog"][d["packet"]]
                        ):
                            raise ObservationError("account_retransmission_binding")
                return dict(
                    head=dict(
                        sequence=stored.head.sequence, event_hash=stored.head.event_hash
                    ),
                    state_hash=stored.current_state.sha256,
                    cursor=dict(
                        index=state["index"],
                        phase=state["phase"],
                        status=state["status"],
                        input_head=state["input_head"],
                    ),
                    accepted_packets=accepted,
                    replay="not_performed",
                )

        def account_head():
            if account is None:
                raise CheckIssue("unverified", "saved_account_not_specified")
            path = root / account
            if root not in path.resolve().parents:
                raise ObservationError("account_outside_trial")
            with read_account(path, files) as stored:
                return dict(
                    sequence=stored.head.sequence, event_hash=stored.head.event_hash
                )

        for which in STAGES if stage == "all" else (stage,):
            checks = []

            def add(key, fn, deps=(), required=True, which=which, checks=checks):
                checks.append(run(which, key, fn, deps, required))

            add("plan", plan)
            add("implementation", implementation, ("plan",))
            add("history", history, ("plan",))
            add("lot", lot, ("history",))
            add("receipt", receipt, ("plan",))
            if which == "acquisition":
                add("acquisition_permission", acquisition_grant, ("plan",))
                add("acquisition_provenance", acquisition_provenance, ("plan",))
                add("date_window", time_window, ("plan",))
                add("official_range", official_range, ("plan",))
                add("budget_time", budget_time, ("receipt",))
                add("budget_attempts", attempt_budget, ("receipt",))
                add("api_key_presence", key_presence)
                add("authentication", authentication, ("receipt",), False)
            else:
                add("responses", captures, ("receipt",))

                def calendar_check():
                    rows = values["responses"][0]["data"]
                    june.sessions(rows, "2026-05-29", "2026-07-01")
                    old = values["history"][2]
                    if sorted(
                        (r for r in old if r["Date"] >= "2026-05-29"),
                        key=lambda r: r["Date"],
                    ) != sorted(
                        (r for r in rows if r["Date"] < "2026-06-01"),
                        key=lambda r: r["Date"],
                    ):
                        raise ObservationError("calendar_revision")
                    return june.split_sessions(
                        [r for r in rows if r["Date"] >= "2026-06-01"]
                    )

                def master_check():
                    rows = values["responses"][1]["data"]
                    if len(rows) != 1 or any(
                        rows[0].get(k) != v
                        for k, v in dict(
                            Code="46890", Date="2026-06-01", ProdCat="011"
                        ).items()
                    ):
                        raise ObservationError("master_mismatch")
                    return dict(symbol="46890", session="2026-06-01")

                add("calendar", calendar_check, ("responses", "history"))
                add("master", master_check, ("responses",))
                add(
                    "history_comparison",
                    lambda: june.compare_history(
                        values["history"][1], values["responses"][2]
                    ),
                    ("responses", "history"),
                )

                def prices():
                    parts = values["calendar"]
                    rows = june.daily(
                        values["responses"][3], "46890", parts[0] + parts[1]
                    )
                    return dict(
                        sessions=len(rows),
                        price_basis="jquants_reported_separate_adjusted",
                    )

                add("price_sessions", prices, ("calendar", "responses"))
                add(
                    "capture_contract",
                    capture_contract,
                    ("calendar", "master", "history_comparison", "price_sessions"),
                )
                add(
                    "indicators",
                    lambda: finite_prefixes(
                        values["history"], values["capture_contract"]
                    ),
                    ("capture_contract",),
                )
                if which in ("clearing", "resume_account"):
                    add(
                        "input_manifest",
                        input_manifest,
                        ("capture_contract", "indicators"),
                    )
                    add("clearing_plan", clearing_plan, ("input_manifest",))
                    add("clearing_permission", clearing_grant, ("clearing_plan",))
                    if which == "resume_account" or account is not None:
                        add("account_head", account_head)
                        add(
                            "account_identity",
                            account_state,
                            ("clearing_plan", "account_head"),
                        )
                    else:

                        def no_account():
                            raise CheckIssue(
                                "not_applicable", "new_account_not_created_by_preflight"
                            )

                        add("new_account_identity", no_account, required=False)
            stages.append(StageReadinessResult(which, tuple(checks)).to_dict())
        files.verify()
        payload = JsonObject.from_value(
            dict(
                schema="preflight-checks-v1",
                checked_at=stamp,
                diagnostic_only=True,
                execution_gate_replacement=False,
                execution_invoked=False,
                authorization_changed=False,
                formal_oos=False,
                stages=stages,
                plan_hash=values["plan"].sha256 if "plan" in values else None,
                acquisition_head=values["receipt"].head
                if "receipt" in values
                else None,
                history_observations=len(values["history"][1].rows.to_dict())
                if "history" in values
                else None,
            )
        )
        return PreflightBundle(payload, files, root, may)
