"""Existing Phase 1 detector/scorer/strategy on a detached adjusted-price view."""

from dataclasses import dataclass

from config.settings import StrategyConfig
from strategy.base import PositionContext
from walkforward.candidates import ExecutableCandidateCatalog

from .replay_policy import fingerprint
from .validation import ReplayContractError


@dataclass(frozen=True, slots=True)
class SignalDecision:
    action: str
    exit_reason: str | None
    range_score: float
    holding_sessions: int
    breakdown_streak: int
    signal_entry_price: str | None


@dataclass(frozen=True, slots=True)
class SignalAdapter:
    base_config: StrategyConfig
    catalog: ExecutableCandidateCatalog

    def config(self, candidate_id):
        matches = [c for c in self.catalog.candidates if c.candidate_id == candidate_id]
        if len(matches) != 1:
            raise ReplayContractError("unknown_entry_candidate")
        return matches[0].apply(self.base_config)

    def config_hash(self, candidate_id):
        return fingerprint(self.config(candidate_id))

    def decide(self, signal_bars, candidate_id, run_start, position_state):
        """Only date + adjusted OHLCV are accepted; raw execution data absent."""
        if set(signal_bars) != {"date", "open", "high", "low", "close", "volume"}:
            raise ReplayContractError("signal_lane_columns_required")
        config = self.config(candidate_id)
        signal = signal_bars.copy().sort_values("date").reset_index(drop=True)
        signal["turnover_value"] = signal["close"] * signal["volume"]
        features = config.create_scorer().transform(
            config.create_detector().transform(signal)
        )
        trading = features.loc[features.date.dt.date >= run_start].copy()
        prepared = config.create_strategy().prepare(trading)
        if prepared.empty:
            raise ReplayContractError("no_completed_signal_session")
        row = prepared.iloc[-1]
        entry = None
        holding = 0
        if position_state is not None:
            holding = position_state["holding_sessions"] + 1
            entry = position_state["signal_entry_price"]
            if entry is None:
                # Phase 1 entry basis = adjusted Open * (1 + buy slippage).
                # Retrieved only at first completed holding close, not at Open.
                entry_rows = signal.loc[
                    signal.date.dt.strftime("%Y-%m-%d")
                    == position_state["entry_session"]
                ]
                if len(entry_rows) != 1:
                    raise ReplayContractError("missing_entry_signal_basis")
                entry = repr(
                    float(entry_rows.iloc[0]["open"]) * (1 + config.slippage_pct)
                )
        context = (
            PositionContext()
            if entry is None
            else PositionContext(True, float(entry), holding)
        )
        answer = config.create_strategy().generate_signal(row, context)
        score = float(row.range_score)
        # No-entry NaN warm-up is not an order with a fabricated score.
        return SignalDecision(
            answer.action.value,
            None if answer.exit_reason is None else answer.exit_reason.value,
            score,
            holding,
            int(row.range_breakdown_streak),
            entry,
        )
