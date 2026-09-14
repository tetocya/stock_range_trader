"""June-only saved-input research account. Never acquires market data."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from delayed_replay import june_trial as june
from delayed_replay.daily_evidence import numeric
from delayed_replay.input_artifacts import InputArtifactStore
from delayed_replay.limited_trial.inputs import SavedProxyInputs
from delayed_replay.limited_trial.models import (
    LimitedProxyTrialPlan,
    ScopedResearchAuthorization,
)
from delayed_replay.limited_trial.service import LimitedTrialService, _LimitedReducer
from delayed_replay.selected_trial.contract import SelectedPolicy
from delayed_replay.selected_trial.workflow import write_once
from delayed_replay.serialization import JsonObject, digest, parse_time, time_text
from delayed_replay.validation import ReplayContractError

SCOPE = dict(
    **june.SCOPE,
    fill_kind="simulated_fill",
    purpose="june_limited_research_not_formal_oos",
)


class JuneClearingPlan(LimitedProxyTrialPlan):
    _schema = "june-clearing-plan-v1"

    @staticmethod
    def _valid_scope(scope):
        return scope == SCOPE

    @property
    def policy(self):
        p = self.payload.to_dict()
        return SelectedPolicy(
            JsonObject.from_value(p["terms"]),
            JsonObject.from_value(p["rules"]),
            JsonObject.from_value(self.scope),
        )


def _plan(root, may_root, manifest):
    p = june.load_json(Path(may_root) / "trial_plan.json")
    p.update(
        schema="june-clearing-plan-v1",
        scope=SCOPE,
        model_hash=june.june_implementation_hash(),
        source_identity="june-input-manifest:" + digest(manifest),
        packets=manifest["run_packets"],
        history_packets=manifest["history_packets"],
        captures=dict(
            calendar=manifest["captures"][0],
            master=manifest["captures"][1],
            daily_source=manifest["captures"][3],
            daily_capture=manifest["captures"][3],
        ),
        references=dict(
            lot=dict(review_hash=manifest["lot_review_hash"]),
            price="jquants_reported_separate_adjusted_not_tick_execution",
            halt="unknown_no_independent_halt_feed",
            external_price=None,
        ),
    )
    plan = JuneClearingPlan(JsonObject.from_value(p))
    plan.signals()
    return plan


def prepare_clearing(root, may_root):
    """Validate build-inputs; publish a proposal, never an approval or account."""
    manifest = june.build(root, may_root, verify_only=True)
    plan = _plan(root, may_root, manifest)
    write_once(Path(root) / "clearing_plan.json", plan.payload.to_dict())
    write_once(
        Path(root) / "clearing_authorization.template.json",
        dict(
            schema="june-clearing-authorization-v1",
            plan_hash=plan.sha256,
            acquisition_plan_hash=manifest["plan_hash"],
            input_manifest_hash=digest(manifest),
            permission="clear_only",
            acquisition_permission=False,
            status="not_approved",
            approval_reference=None,
            recorded_at=None,
        ),
    )
    return plan


@dataclass(frozen=True)
class JuneClearingAuthorization:
    grant: JsonObject

    def require(self, plan):
        a = self.grant.to_dict()
        if (
            set(a)
            != {
                "schema",
                "plan_hash",
                "acquisition_plan_hash",
                "input_manifest_hash",
                "permission",
                "acquisition_permission",
                "status",
                "approval_reference",
                "recorded_at",
            }
            or a["schema"] != "june-clearing-authorization-v1"
            or a["permission"] != "clear_only"
            or a["acquisition_permission"] is not False
            or a["plan_hash"] != plan.sha256
            or plan.payload.to_dict()["source_identity"]
            != "june-input-manifest:" + a["input_manifest_hash"]
        ):
            raise ReplayContractError("june_separate_clearing_approval_required")
        june.require_hash(a["acquisition_plan_hash"])
        self.scoped.require(plan)

    @property
    def scoped(self):
        a = self.grant.to_dict()
        # Preserve the actual approval reference in the separately audited grant.
        june.require_text(a["approval_reference"])
        return ScopedResearchAuthorization(
            JsonObject.from_value(
                dict(
                    schema="limited-authorization-v1",
                    plan_hash=a["plan_hash"],
                    status=a["status"],
                    approval_reference="june-clearing-grant:" + self.grant.sha256,
                    recorded_at=a["recorded_at"],
                )
            )
        )


def verified_inputs(plan, root, may_root):
    manifest = june.build(root, may_root, verify_only=True)
    if type(plan) is not JuneClearingPlan or plan != _plan(root, may_root, manifest):
        raise ReplayContractError("june_clearing_plan_or_inputs_changed")
    _, bundle, _ = june.load_context(root, may_root)
    rows = bundle.rows.to_dict()
    for sha in manifest["run_packets"]:
        packet = InputArtifactStore(Path(root) / "inputs").load(sha)
        for snapshot in packet.market.snapshots:
            for bar in snapshot.observations:
                day = bar.session_date.isoformat()
                key = bar.symbol + "|" + day
                if key in rows:
                    raise ReplayContractError("june_duplicate_observation")
                rows[key] = dict(
                    row=dict(
                        symbol=bar.symbol,
                        session=day,
                        **dict(
                            zip(
                                ("open", "high", "low", "close", "volume"),
                                map(numeric, bar.raw_ohlcv),
                                strict=True,
                            )
                        ),
                        adjustment_factor=numeric(bar.adjustment_factor),
                        halt="unknown",
                        volume_unit="execution_shares",
                        actual_trade_at=None,
                    ),
                    adjusted=list(map(numeric, bar.adjusted_ohlcv)),
                    packet=sha,
                    snapshot_hash=snapshot.payload_sha256,
                    first_observed_at=time_text(snapshot.first_observed_at),
                    fetched_at=time_text(snapshot.fetched_at),
                    acquired_at=time_text(snapshot.fetched_at),
                    stock_split=numeric(bar.stock_split),
                    dividend=numeric(bar.dividend),
                    ex_right=None,
                )
    return SavedProxyInputs(
        JsonObject.from_value(rows),
        tuple(d for part in manifest["parts"] for d in part),
        JsonObject.from_value(manifest),
    )


class _SealedInputs:
    """One fully validated immutable bundle per open Service; fail on disk mutation.

    No module/global cache. Resume revalidates all lineage and finite features.
    Hash every evidence file again before each event, without rerunning indicators.
    """

    def __init__(self, bundle, root, may_root):
        self.bundle = bundle
        self.files = {
            p: self._hash(p)
            for directory in (Path(root), Path(may_root))
            for p in directory.rglob("*")
            if p.is_file() and p.suffix in (".json", ".pdf")
        }

    @staticmethod
    def _hash(path):
        if path.is_symlink():
            raise ReplayContractError("june_evidence_symlink")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def load(self, *_):
        if any(self._hash(p) != h for p, h in self.files.items()):
            raise ReplayContractError("june_open_service_evidence_changed")
        return self.bundle


class _JuneReducer(_LimitedReducer):
    identity = "june-daily-open-proxy-reducer-v1"
    _plan_type = JuneClearingPlan

    def _resolution_scope(self, policy):
        return policy.scope_value.to_dict()

    def __call__(self, raw, command):
        identity = raw["identity"]
        grant = JuneClearingAuthorization(
            JsonObject.from_value(identity["june_clearing_authorization"])
        )
        grant.require(JuneClearingPlan(JsonObject.from_value(identity["plan"])))
        if grant.scoped.payload.to_dict() != identity["authorization"]:
            raise ReplayContractError("june_audited_grant_changed")
        return super().__call__(raw, command)


class JuneClearingService(LimitedTrialService):
    """Arguments packet_root/evidence_root mean June root/May history root here."""

    _reducer = _JuneReducer

    @classmethod
    def _prepare(cls, plan, authorization, packet_root, evidence_root):
        if (
            type(plan) is not JuneClearingPlan
            or type(authorization) is not JuneClearingAuthorization
        ):
            raise ReplayContractError("june_typed_plan_and_clearing_grant_required")
        authorization.require(plan)
        manifest = june.load_json(Path(packet_root) / "input_manifest.json")
        if (
            authorization.grant.to_dict()["acquisition_plan_hash"]
            != manifest["plan_hash"]
        ):
            raise ReplayContractError("june_clearing_acquisition_binding")
        return verified_inputs(plan, packet_root, evidence_root)

    @staticmethod
    def _initial(plan, authorization, bundle, style):
        state = LimitedTrialService._initial(
            plan, authorization.scoped, bundle, style
        ).to_dict()
        state["identity"]["june_clearing_authorization"] = authorization.grant.to_dict()
        return JsonObject.from_value(state)

    def __init__(self, store, plan, authorization, packet_root, evidence_root):
        super().__init__(store, plan, authorization, packet_root, evidence_root)
        try:
            bundle = self._prepare(plan, authorization, packet_root, evidence_root)
            self._inputs = _SealedInputs(bundle, packet_root, evidence_root)
        except Exception:
            store.close()
            raise


def comparison_projection(state):
    """Delivery bookkeeping differs; business records and execution cursor must not."""
    orders = state["orders"]
    return dict(
        orders=orders,
        reservations={
            k: dict(
                reserved_cash=o["reserved_cash"],
                frozen_reserved=o["frozen"]["reserved"],
            )
            for k, o in orders.items()
        },
        fills={
            k: o["resolution"] for k, o in orders.items() if o["status"] == "filled"
        },
        cash={
            k: state[k]
            for k in (
                "cash",
                "equity",
                "peak_equity",
                "realized_profit",
                "proceeds_hold",
            )
        },
        positions=state["positions"],
        cursor={
            k: state[k]
            for k in (
                "index",
                "phase",
                "status",
                "reason",
                "selected_before",
                "decided_through",
            )
        },
        decisions=state["decisions"],
        marks=state["marks"],
        epochs=state["epochs"],
        episodes=state["episodes"],
        batches=state["batches"],
        valuation_complete=state["valuation_complete"],
    )


def compare(root, may_root, plan, authorization, output, wall):
    """Explicitly authorized continuous versus waiting/accept/close/reopen trial."""
    JuneClearingService._prepare(plan, authorization, root, may_root)
    if parse_time(authorization.grant.to_dict()["recorded_at"]) > wall:
        raise ReplayContractError("june_authorization_not_yet_recorded")
    output = Path(output)
    if output.exists():
        raise ReplayContractError("june_new_comparison_output_required")
    output.mkdir(parents=True)
    args = (plan, authorization, root, may_root)
    service = JuneClearingService.create(
        output / "continuous.sqlite", *args, "continuous"
    )
    try:
        expected = service.run(wall)
        report = service.report().to_dict()
    finally:
        service.store.close()
    service = JuneClearingService.create(output / "split.sqlite", *args, "split_resume")
    try:
        waiting = service.run(wall)
        if waiting["status"] != "waiting_for_input":
            raise ReplayContractError("june_split_must_wait")
        prefix = service.store.read().events
        packet = plan.payload.to_dict()["packets"][1]
        service.accept(packet, "june-part-2", waiting["input_head"], wall)
        service.accept(packet, "june-part-2", waiting["input_head"], wall)
        accepted = service.state
    finally:
        service.store.close()
    service = JuneClearingService.resume(output / "split.sqlite", *args, "split_resume")
    try:
        restored = service.state == accepted
        actual = service.run(wall)
        prefix_equal = service.store.read().events[: len(prefix)] == prefix
    finally:
        service.store.close()
    left, right = comparison_projection(expected), comparison_projection(actual)
    matches = {k: left[k] == right[k] for k in left}
    if (
        not all(matches.values())
        or not restored
        or not prefix_equal
        or actual["status"] != "completed"
    ):
        raise ReplayContractError("june_comparison_incomplete_or_mismatch")
    result = dict(
        schema="june-comparison-v1",
        plan_hash=plan.sha256,
        settings_hash=june.SETTINGS_HASH,
        implementation_hash=june.june_implementation_hash(),
        input_manifest_hash=plan.payload.to_dict()["source_identity"].split(":")[1],
        provenance=plan.payload.to_dict()["provenance"],
        matches=matches,
        continuous_hash=digest(left),
        resumed_hash=digest(right),
        accepted_state_restored=restored,
        prefix_unchanged=prefix_equal,
        waiting_cursor=comparison_projection(waiting)["cursor"],
        report=report,
        real_data_clearing="unverified"
        if plan.payload.to_dict()["provenance"] == "artificial_fixture"
        else "limited_research_only",
        actual_trade_at=None,
        formal_oos=False,
        formal_checkpoint="unsupported",
    )
    write_once(output / "comparison.json", result)
    return result
