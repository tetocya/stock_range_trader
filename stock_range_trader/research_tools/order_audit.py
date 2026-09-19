"""Saved order audit: no signals, selection, clearing, recovery or network calls."""

import argparse
import json
from dataclasses import dataclass
from decimal import localcontext
from pathlib import Path

from delayed_replay.input_artifacts import InputPacket
from delayed_replay.proxy.resolution import FrozenProxyOrder
from delayed_replay.serialization import JsonObject, digest, require_hash, time_text

from .arithmetic import OrderArithmeticVerifier, number, text
from .reader import EvidenceFiles, ObservationError, read_account

PLAN_SCHEMAS = {
    "limited-proxy-plan-v1",
    "selected-trial-plan-v1",
    "june-clearing-plan-v1",
}


@dataclass(frozen=True)
class OrderAuditRecord:
    payload: JsonObject

    def to_dict(self):
        return self.payload.to_dict()


@dataclass(frozen=True)
class OrderAuditBundle:
    records: tuple[OrderAuditRecord, ...]
    metadata: JsonObject
    files: EvidenceFiles
    input_root: Path


def _price_evidence(root, state, symbol, day, files, cache):
    item = state["inputs"].get(symbol + "|" + day)
    if item is None:
        return None, [], ["missing_saved_bar:" + day]
    if item["row"]["symbol"] != symbol or item["row"]["session"] != day:
        raise ObservationError("saved_bar_key_mismatch")
    sha = item["packet"]
    require_hash(sha)
    try:
        if sha not in cache:
            payload = files.json(root / "inputs" / (sha + ".json"), sha)
            cache[sha] = InputPacket.from_payload(payload)
        packet = cache[sha]
        snapshots = [
            s
            for s in packet.market.snapshots
            if s.payload_sha256 == item["snapshot_hash"]
        ]
        if len(snapshots) != 1:
            raise ObservationError("snapshot_reference_mismatch")
        snapshot = snapshots[0]
        bars = [
            b
            for b in snapshot.observations
            if b.symbol == symbol and b.session_date.isoformat() == day
        ]
        if len(bars) != 1:
            raise ObservationError("packet_bar_missing_or_duplicate")
        bar = bars[0]
        saved = (
            [item["row"][k] for k in ("open", "high", "low", "close", "volume")]
            + item["adjusted"]
            + [item["row"]["adjustment_factor"]]
        )
        values = (
            list(bar.raw_ohlcv) + list(bar.adjusted_ohlcv) + [bar.adjustment_factor]
        )
        if any(number(a) != number(str(b)) for a, b in zip(saved, values, strict=True)):
            raise ObservationError("saved_packet_price_mismatch")
        if item["fetched_at"] != time_text(snapshot.fetched_at) or item[
            "first_observed_at"
        ] != time_text(snapshot.first_observed_at):
            raise ObservationError("saved_packet_timestamp_mismatch")
        source_hash = snapshot.source_artifact_sha256
        source = files.json(root / (source_hash + ".json"), source_hash).to_dict()
        source_rows = [
            r for r in source["data"] if r["Code"] == symbol and r["Date"] == day
        ]
        if len(source_rows) != 1:
            raise ObservationError("source_bar_missing_or_duplicate")
        fields = (
            "O",
            "H",
            "L",
            "C",
            "Vo",
            "AdjO",
            "AdjH",
            "AdjL",
            "AdjC",
            "AdjVo",
            "AdjFactor",
        )
        if any(
            number(str(source_rows[0][k])) != number(v)
            for k, v in zip(fields, saved, strict=True)
        ):
            raise ObservationError("source_price_mismatch")
        return item["row"], [sha, snapshot.payload_sha256, source_hash], []
    except ObservationError as error:
        if str(error) != "missing_input":
            raise
        return (
            item["row"],
            [sha, item["snapshot_hash"]],
            ["missing_external_price_evidence:" + day],
        )


class OrderAuditReader:
    def read(self, trial_root, account=None, *, _observer=None):
        root = Path(trial_root).absolute()
        files = EvidenceFiles()
        names = [
            root / n
            for n in ("trial_plan.json", "clearing_plan.json")
            if (root / n).exists()
        ]
        if len(names) != 1:
            raise ObservationError("one_saved_trial_plan_required")
        plan_object = files.json(names[0])
        plan = plan_object.to_dict()
        if plan.get("schema") not in PLAN_SCHEMAS:
            raise ObservationError("unsupported_trial_schema")
        if account is None:
            # Existing May comparison layout; never choose among arbitrary DBs.
            account = "comparison/continuous.sqlite"
        db = root / account
        if root not in db.resolve().parents:
            raise ObservationError("account_must_belong_to_trial")
        with read_account(db, files) as stored, localcontext() as arithmetic_context:
            arithmetic_context.prec = 128
            state, initial = (
                stored.current_state.to_dict(),
                stored.initial_state.to_dict(),
            )
            if (
                state.get("schema") != "limited-proxy-state-v1"
                or initial.get("schema") != state["schema"]
            ):
                raise ObservationError("unsupported_account_schema")
            identity = state["identity"]
            if (
                identity != initial["identity"]
                or digest(identity) != stored.identity.config_hash
                or identity["plan"] != plan
                or identity["plan_hash"] != plan_object.sha256
                or stored.identity.protocol_hash != plan_object.sha256
            ):
                raise ObservationError("saved_trial_identity_mismatch")
            if not isinstance(state["orders"], dict) or not isinstance(
                state["inputs"], dict
            ):
                raise ObservationError("invalid_account_structure")
            orders, cache = [], {}
            for event in stored.events:
                payload = event.command.payload.to_dict()
                if payload.get("schema") != "limited-proxy-event-v1" or payload.get(
                    "action"
                ) not in ("phase", "extension"):
                    raise ObservationError("unsupported_saved_event_schema")
            cash_delta = number("0")
            cash_known = True
            for oid, order in state["orders"].items():
                o = FrozenProxyOrder(
                    JsonObject.from_value(order["frozen"])
                ).value.to_dict()
                if oid != o["order_id"]:
                    raise ObservationError("order_id_mismatch")
                status = order["status"]
                if status not in (
                    "pending",
                    "filled",
                    "rejected",
                    "cancelled",
                    "canceled",
                    "waiting",
                ):
                    raise ObservationError("unsupported_saved_order_status")
                resolution = order.get("resolution")
                if resolution is not None and set(resolution) != {
                    "order_id",
                    "filled",
                    "reason",
                    "price",
                    "gross",
                    "commission",
                    "net",
                    "shares",
                    "actual_trade_at",
                }:
                    raise ObservationError("unsupported_resolution_fields")
                reference, ref_hashes, missing = _price_evidence(
                    root, state, o["symbol"], o["decision_session"], files, cache
                )
                target, target_hashes, target_missing = _price_evidence(
                    root, state, o["symbol"], o["target"], files, cache
                )
                opening = target["open"] if target else None
                verification = OrderArithmeticVerifier().verify(
                    o,
                    plan.get("terms", {}),
                    plan["scope"].get("model_id"),
                    opening,
                    resolution,
                    status,
                )
                verification["missing_evidence"].extend(missing + target_missing)
                if reference and number(reference["close"]) != number(
                    o["reference_price"]
                ):
                    verification["mismatches"].append("reference_close")
                if verification["mismatches"]:
                    verification["status"] = "mismatch"
                elif (
                    verification["missing_evidence"]
                    and verification["status"] == "verified"
                ):
                    verification["status"] = "insufficient_evidence"
                decision_events, resolution_events = [], []
                for event in stored.events:
                    payload = event.command.payload.to_dict()
                    if payload.get("schema") != "limited-proxy-event-v1":
                        raise ObservationError("unsupported_saved_event_schema")
                    if payload["action"] != "phase":
                        continue
                    phase, day = (
                        payload["data"]["phase"],
                        event.command.market_decision_at.date().isoformat(),
                    )
                    if phase == "decide" and day == o["decision_session"]:
                        decision_events.append(event)
                    if phase == "resolve" and day == o["target"]:
                        resolution_events.append(event)
                if not decision_events:
                    verification["missing_evidence"].append("decision_event")
                    if verification["status"] == "verified":
                        verification["status"] = "insufficient_evidence"
                if status in ("filled", "rejected") and not resolution_events:
                    verification["missing_evidence"].append("resolution_event")
                    if verification["status"] == "verified":
                        verification["status"] = "insufficient_evidence"
                if (
                    status == "filled"
                    and resolution is not None
                    and resolution.get("filled") is True
                ):
                    charged = number(resolution["commission"])
                    change = number(resolution["net"]) * (
                        -1 if o["side"] == "BUY" else 1
                    )
                elif status != "filled" and (
                    (
                        resolution is None
                        and status in ("pending", "waiting", "cancelled", "canceled")
                    )
                    or (resolution is not None and resolution.get("filled") is False)
                ):
                    charged, change = number("0"), number("0")
                else:
                    charged, change, cash_known = None, None, False
                if change is not None:
                    cash_delta += change
                row = dict(
                    order_id=oid,
                    symbol=o["symbol"],
                    side=o["side"],
                    sequence=decision_events[0].sequence if decision_events else None,
                    decision_session=o["decision_session"],
                    decision_phase=o["decision_phase"],
                    decision_market_at=time_text(
                        decision_events[0].command.market_decision_at
                    )
                    if decision_events
                    else None,
                    decision_replayed_at=time_text(
                        decision_events[0].command.replayed_at
                    )
                    if decision_events
                    else None,
                    target_session=o["target"],
                    candidate_id=o["candidate_id"],
                    config_hash=o["config_hash"],
                    fixed_shares=o["shares"],
                    reference_price=o["reference_price"],
                    decision_equity=o["decision_equity"],
                    fixed_budget=o["budget"],
                    fixed_reserved_amount=o["reserved"],
                    current_reserved_cash=order["reserved_cash"],
                    current_reserved_shares=order["reserved_shares"],
                    sell_fixed_share_reservation=o["shares"]
                    if o["side"] == "SELL"
                    else None,
                    share_reservation_basis="v1_full_quantity_contract"
                    if o["side"] == "SELL"
                    else None,
                    target_open=opening,
                    saved_status=status,
                    saved_reason=resolution.get("reason")
                    if resolution
                    else order.get("reason"),
                    account_wait_reason=state["reason"]
                    if state["status"] == "waiting_for_input"
                    else None,
                    saved_resolution=resolution,
                    actual_cash_change=text(change),
                    charged_commission=text(charged),
                    cash_change_basis="saved_resolution_allocation_not_replayed_transition",
                    sell_proceeds_hold_on_fill=text(change)
                    if o["side"] == "SELL" and status == "filled"
                    else None,
                    proceeds_mode=plan.get("terms", {}).get("proceeds_mode"),
                    verification=verification,
                    decision_event_ids=[e.command.event_id for e in decision_events],
                    resolution_event_ids=[
                        e.command.event_id for e in resolution_events
                    ],
                    event_link_basis="session_phase_association_not_order_replay",
                    evidence_hashes=sorted(set(ref_hashes + target_hashes)),
                    fill_kind="simulated_fill",
                    actual_trade_at=None,
                )
                orders.append(OrderAuditRecord(JsonObject.from_value(row)))
            orders.sort(
                key=lambda r: (
                    r.to_dict()["sequence"] is None,
                    r.to_dict()["sequence"] or 0,
                    r.to_dict()["order_id"],
                )
            )
            cash_match = (
                (number(initial["cash"]) + cash_delta == number(state["cash"]))
                if cash_known
                else None
            )
            metadata = JsonObject.from_value(
                dict(
                    schema="order-audit-read-v1",
                    trial_id=plan_object.sha256,
                    stream_id=stored.identity.stream_id,
                    genesis_hash=stored.identity.genesis_hash,
                    read_head=dict(
                        sequence=stored.head.sequence, event_hash=stored.head.event_hash
                    ),
                    state_hash=stored.current_state.sha256,
                    input_head=state["input_head"],
                    as_of_replayed_at=time_text(stored.events[-1].command.replayed_at)
                    if stored.events
                    else None,
                    scope={
                        k: plan["scope"].get(k)
                        for k in ("symbol", "start", "end", "model_id", "mode")
                    },
                    recorded_execution_model_hash=plan["model_hash"],
                    settings_hash=digest(
                        {k: plan[k] for k in ("strategy", "terms", "rules")}
                    ),
                    provenance=plan["provenance"],
                    order_count=len(orders),
                    account_cash_reconciliation=cash_match,
                    initial_cash=initial["cash"],
                    final_cash=state["cash"],
                    allocated_order_cash_change=text(cash_delta)
                    if cash_known
                    else None,
                    validation="hash_chain_and_saved_state_only_no_transition_replay",
                    authenticity="not_proven_by_hashes",
                    read_only=True,
                    formal_oos=False,
                    formal_checkpoint="not_assessed",
                    grants_created=False,
                )
            )
            # Internal projection hook: the viewer shares this exact DB transaction
            # and evidence set; never opens a second, potentially newer snapshot.
            if _observer is not None:
                _observer(state, stored, root, files, cache)
        return OrderAuditBundle(tuple(orders), metadata, files, root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--account", help="Relative DB path; default comparison/continuous.sqlite"
    )
    args = parser.parse_args(argv)
    try:
        from .order_report import OrderAuditReportWriter

        bundle = OrderAuditReader().read(args.trial_root, args.account)
        OrderAuditReportWriter().write(bundle, args.output)
        print(
            json.dumps(
                dict(
                    status="report_written_not_execution",
                    order_count=len(bundle.records),
                )
            )
        )
        return 0
    except (ValueError, OSError, KeyError, TypeError, OverflowError):
        print(
            json.dumps(
                dict(
                    status="blocked",
                    reason="input_schema_integrity_or_output_contract",
                    execution="not_invoked",
                )
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
