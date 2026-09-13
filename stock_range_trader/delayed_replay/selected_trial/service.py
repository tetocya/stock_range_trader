"""Selected-instrument entry; legacy 72030 and formal Gates remain closed."""

import math
from datetime import date

import numpy as np
import pandas as pd

from delayed_replay.limited_trial.inputs import SavedProxyInputs
from delayed_replay.limited_trial.preflight import LimitedTrialPreflight
from delayed_replay.limited_trial.service import LimitedTrialService, _LimitedReducer
from delayed_replay.serialization import JsonObject, digest
from delayed_replay.validation import ReplayContractError

from .acquisition import stage_a, stage_b
from .contract import AcquisitionPlan, SelectedTrialPlan
from .pipeline import daily, read, select, sessions


def generated_rows(days, symbol):
    """Public deterministic recipe proves artificial provenance, not just a flag."""
    from delayed_replay.daily_evidence import numeric

    result = {}
    for day in days:
        n = date.fromisoformat(day).toordinal()
        price = round(100 + 4 * math.cos(n * math.pi / 10), 2)
        result[day] = dict(
            Date=day,
            Code=symbol,
            O=price,
            H=round(price + 0.2, 2),
            L=round(price - 0.2, 2),
            C=price,
            Vo=2000000,
            AdjO=price,
            AdjH=round(price + 0.2, 2),
            AdjL=round(price - 0.2, 2),
            AdjC=price,
            AdjVo=2000000,
            AdjFactor=1,
            ExRT=None,
        )
    return {
        d: {
            k: numeric(v) if k not in ("Date", "Code", "ExRT") else v
            for k, v in r.items()
        }
        for d, r in result.items()
    }


class SelectedInputs(SavedProxyInputs):
    @staticmethod
    def _generated_rows(source, symbol):
        from delayed_replay.daily_evidence import numeric

        result = generated_rows(source["fixture_sessions"], symbol)
        # Legacy artificial lane comparison uses canonical decimal strings.
        return {
            d: {
                k: numeric(v) if k not in ("Date", "Code", "ExRT") else v
                for k, v in r.items()
            }
            for d, r in result.items()
        }

    @classmethod
    def load(cls, plan, packet_root, evidence_root):
        if type(plan) is not SelectedTrialPlan:
            raise ReplayContractError("selected_plan_required")
        p = plan.payload.to_dict()
        if not p["source_identity"].startswith("selected-binding:"):
            raise ReplayContractError("selected_binding_required")
        binding = read(evidence_root, p["source_identity"].split(":")[1])
        acquisition = AcquisitionPlan(JsonObject.from_value(binding["acquisition"]))
        ap = acquisition.payload.to_dict()
        a, b, selection = binding["stage_a"], binding["stage_b"], binding["selection"]
        if (
            acquisition.sha256 != plan.scope["acquisition_hash"]
            or digest(selection) != plan.scope["selection_hash"]
            or selection["symbol"] != plan.scope["symbol"]
            or select(acquisition, a, evidence_root) != selection
        ):
            raise ReplayContractError("selected_acquisition_or_selection_changed")
        if (
            any(p[k] != ap[k] for k in ("terms", "rules", "strategy", "model_hash"))
            or p["candidates"] != [ap["candidate"]]
            or p["candidate_id"] != "baseline"
        ):
            raise ReplayContractError("selected_approved_settings_changed")
        artificial = p["provenance"] == "artificial_fixture"
        if binding["origin"] != (
            "generated_selected_v1" if artificial else "saved_jquants"
        ):
            raise ReplayContractError("selected_provenance_mismatch")
        qs = stage_b(selection["symbol"])
        if set(b) != {digest(q) for q in qs}:
            raise ReplayContractError("selected_stage_b_scope")
        calendar, master, history, run = [b[digest(q)] for q in qs]
        all_dates = sessions(calendar["data"], "2026-01-01", "2026-06-01")
        if a[digest(stage_a()[0])]["data"][0] not in calendar["data"]:
            raise ReplayContractError("selected_calendar_revision")
        if artificial:
            for c in list(a.values()) + list(b.values()):
                if c["query"]["path"].endswith("bars/daily"):
                    expected = generated_rows(
                        [r["Date"] for r in c["data"]], c["query"]["params"]["code"]
                    )
                    if any(expected[r["Date"]] != r for r in c["data"]):
                        raise ReplayContractError("selected_artificial_recipe_mismatch")
        source = read(evidence_root, p["captures"]["daily_source"])
        if (
            source["data"] != run["data"]
            or read(evidence_root, p["captures"]["master"])["data"] != master["data"]
        ):
            raise ReplayContractError("selected_source_capture_mismatch")
        if read(evidence_root, p["captures"]["calendar"])["data"] != [
            r for r in calendar["data"] if r["Date"] >= "2026-05-01"
        ]:
            raise ReplayContractError("selected_calendar_capture_mismatch")
        bundle = super().load(plan, packet_root, evidence_root)
        symbol = selection["symbol"]
        reference = next(
            c
            for c in a.values()
            if c["query"]["path"].endswith("bars/daily")
            and c["query"]["params"].get("code") == symbol
        )
        expected_rows = {}
        for c, days in (
            (history, tuple(d for d in all_dates if d < "2026-04-30")),
            (reference, ("2026-04-30",)),
            (run, tuple(d for d in all_dates if d >= "2026-05-01")),
        ):
            for day, v in daily(c, symbol, days).items():
                expected_rows[symbol + "|" + day] = (v, c)
        if set(bundle.rows.to_dict()) != set(expected_rows):
            raise ReplayContractError("selected_history_scope")
        snapshots = {
            s["snapshot_hash"]: s for s in bundle.inventory.to_dict()["snapshots"]
        }
        for k, row in bundle.rows.to_dict().items():
            v, c = expected_rows[k]
            raw = [
                row["row"][name] for name in ("open", "high", "low", "close", "volume")
            ]
            if (
                raw + row["adjusted"] + [row["row"]["adjustment_factor"]] != v["values"]
                or row["fetched_at"] != c["fetched_at"]
                or row["first_observed_at"] != c["fetched_at"]
            ):
                raise ReplayContractError("selected_history_lineage")
            expected_source = p["captures"]["daily_source"] if c is run else digest(c)
            if snapshots[row["snapshot_hash"]]["source_hash"] != expected_source:
                raise ReplayContractError("selected_snapshot_source_hash")
        return bundle


class SelectedPreflight(LimitedTrialPreflight):
    _inputs = SelectedInputs

    @classmethod
    def evaluate(cls, plan, packet_root, evidence_root, authorization=None):
        result = (
            super().evaluate(plan, packet_root, evidence_root, authorization).to_dict()
        )
        if result["ready"]:
            bundle = cls._inputs.load(plan, packet_root, evidence_root)
            config = plan.signals().config("baseline")
            rows = bundle.rows.to_dict()
            for day in bundle.sessions:
                frame = pd.DataFrame(
                    [
                        dict(
                            date=pd.Timestamp(v["row"]["session"]),
                            **dict(
                                zip(
                                    ("open", "high", "low", "close", "volume"),
                                    map(float, v["adjusted"]),
                                    strict=True,
                                )
                            ),
                        )
                        for v in rows.values()
                        if v["row"]["session"] <= day
                    ]
                ).sort_values("date")
                frame["turnover_value"] = frame.close * frame.volume
                features = config.create_scorer().transform(
                    config.create_detector().transform(frame)
                )
                if not np.isfinite(
                    features.iloc[-1][["sma", "atr", "adx", "range_score"]].to_numpy(
                        dtype=float
                    )
                ).all():
                    result.update(ready=False, status="blocked")
                    result["reasons"].append("nonfinite_features")
                    break
            result["checks"]["causal_features"] = dict(
                status="verified" if result["ready"] else "blocked",
                reason="each_session_prefix_only_no_signal_or_clearing",
            )
        result["schema"] = "selected-preflight-v1"
        return JsonObject.from_value(result)


class SelectedReducer(_LimitedReducer):
    identity = "selected-daily-open-proxy-reducer-v1"
    _plan_type = SelectedTrialPlan

    def _resolution_scope(self, policy):
        return policy.scope_value.to_dict()


class SelectedService(LimitedTrialService):
    _inputs = SelectedInputs
    _preflight = SelectedPreflight
    _reducer = SelectedReducer
