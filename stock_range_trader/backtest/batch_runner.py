"""Independent per-symbol Phase 1.1 backtests for Phase 2 rankings."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from data import canonical_to_phase1, require_single_provider
from metrics import calculate_backtest_metrics
from universe import jquants_to_yfinance

if TYPE_CHECKING:
    from config import StrategyConfig

BATCH_SUMMARY_COLUMNS: tuple[str, ...] = (
    "symbol",
    "company_name",
    "provider",
    "status",
    "error",
    "period_start",
    "period_end",
    "range_score_as_of",
    "total_return",
    "cagr",
    "maximum_drawdown",
    "sharpe_ratio",
    "sortino_ratio",
    "number_of_trades",
    "win_rate",
    "profit_factor",
    "average_holding_period",
    "exposure",
    "executable_buy_and_hold_return",
    "strategy_vs_executable_buy_and_hold",
    "filled_orders",
    "rejected_orders",
    "canceled_orders",
)


@dataclass(frozen=True, slots=True)
class BatchBacktestResult:
    """Cross-symbol summary plus concatenated detailed logs."""

    provider: str
    summary: pd.DataFrame
    trade_log: pd.DataFrame
    order_log: pd.DataFrame

    def save(self, output_dir: str | Path) -> tuple[Path, Path, Path]:
        directory = Path(output_dir)
        directory.mkdir(parents=True, exist_ok=True)
        summary_path = directory / "batch_backtest_summary.csv"
        trades_path = directory / "batch_trade_log.csv"
        orders_path = directory / "batch_order_log.csv"
        self.summary.to_csv(summary_path, index=False)
        self.trade_log.to_csv(trades_path, index=False)
        self.order_log.to_csv(orders_path, index=False)
        return summary_path, trades_path, orders_path


@dataclass(frozen=True, slots=True)
class BatchBacktestRunner:
    """Reuse the unchanged single-stock engine without shared portfolio capital."""

    strategy_config: StrategyConfig

    def run(self, ranking: pd.DataFrame, bars: pd.DataFrame) -> BatchBacktestResult:
        required = {"symbol", "company_name", "provider", "range_score"}
        missing = sorted(required - set(ranking.columns))
        if missing:
            raise ValueError("ranking missing columns: " + ", ".join(missing))
        ranking_providers = set(ranking["provider"].dropna().astype(str))
        if len(ranking_providers) != 1:
            raise ValueError("ranking must use exactly one provider")
        provider = next(iter(ranking_providers))
        if not bars.empty and require_single_provider(bars) != provider:
            raise ValueError("ranking and bars must use exactly the same provider")
        if "as_of_date" in ranking:
            ranking_dates = set(pd.to_datetime(ranking["as_of_date"]).dt.date)
            if len(ranking_dates) != 1:
                raise ValueError("ranking must contain exactly one as_of_date")
            if (
                not bars.empty
                and (bars["date"].dt.date > next(iter(ranking_dates))).any()
            ):
                raise ValueError("bars after ranking as_of_date are forbidden")

        summary_records: list[dict[str, object]] = []
        trade_frames: list[pd.DataFrame] = []
        order_frames: list[pd.DataFrame] = []
        sort_columns = [column for column in ("rank", "symbol") if column in ranking]
        candidates = ranking.sort_values(sort_columns, kind="stable")
        for _, candidate in candidates.iterrows():
            symbol = str(candidate["symbol"])
            company_name = str(candidate["company_name"])
            try:
                provider_symbol = (
                    jquants_to_yfinance(symbol) if provider == "yfinance" else symbol
                )
                symbol_bars = bars.loc[
                    bars["symbol"].astype(str) == provider_symbol
                ].copy()
                phase1 = canonical_to_phase1(symbol_bars, symbol=provider_symbol)
                detected = self.strategy_config.create_detector().transform(phase1)
                scored = self.strategy_config.create_scorer().transform(detected)
                result = self.strategy_config.create_engine().run(symbol, scored)
                metrics = calculate_backtest_metrics(
                    result,
                    annual_trading_days=self.strategy_config.annual_trading_days,
                    risk_free_rate=self.strategy_config.risk_free_rate,
                )
            except Exception as error:
                summary_records.append(_failed_summary(candidate, provider, str(error)))
                continue

            status_counts = result.order_log["status"].value_counts()
            summary_records.append(
                {
                    "symbol": symbol,
                    "company_name": company_name,
                    "provider": provider,
                    "status": "ok",
                    "error": "",
                    "period_start": phase1["date"].iloc[0],
                    "period_end": phase1["date"].iloc[-1],
                    "range_score_as_of": float(candidate["range_score"]),
                    "total_return": metrics.total_return,
                    "cagr": metrics.cagr,
                    "maximum_drawdown": metrics.maximum_drawdown,
                    "sharpe_ratio": metrics.sharpe_ratio,
                    "sortino_ratio": metrics.sortino_ratio,
                    "number_of_trades": metrics.number_of_trades,
                    "win_rate": metrics.win_rate,
                    "profit_factor": metrics.profit_factor,
                    "average_holding_period": metrics.average_holding_period,
                    "exposure": metrics.exposure,
                    "executable_buy_and_hold_return": (
                        metrics.executable_buy_and_hold_return
                    ),
                    "strategy_vs_executable_buy_and_hold": (
                        metrics.strategy_vs_executable_buy_and_hold
                    ),
                    "filled_orders": int(status_counts.get("filled", 0)),
                    "rejected_orders": int(status_counts.get("rejected", 0)),
                    "canceled_orders": int(status_counts.get("canceled", 0)),
                }
            )
            trade_frames.append(
                _annotate_log(result.trade_log, symbol, company_name, provider)
            )
            order_frames.append(
                _annotate_log(result.order_log, symbol, company_name, provider)
            )

        summary = pd.DataFrame.from_records(
            summary_records, columns=BATCH_SUMMARY_COLUMNS
        )
        trades = _concat_logs(trade_frames)
        orders = _concat_logs(order_frames)
        return BatchBacktestResult(provider, summary, trades, orders)


def _failed_summary(
    candidate: pd.Series, provider: str, message: str
) -> dict[str, object]:
    record: dict[str, object] = {
        column: float("nan") for column in BATCH_SUMMARY_COLUMNS
    }
    record.update(
        {
            "symbol": str(candidate["symbol"]),
            "company_name": str(candidate["company_name"]),
            "provider": provider,
            "status": "failed",
            "error": message,
            "range_score_as_of": candidate["range_score"],
            "number_of_trades": 0,
            "filled_orders": 0,
            "rejected_orders": 0,
            "canceled_orders": 0,
        }
    )
    return record


def _annotate_log(
    frame: pd.DataFrame, symbol: str, company_name: str, provider: str
) -> pd.DataFrame:
    result = frame.copy()
    result.insert(1, "company_name", company_name)
    result.insert(2, "provider", provider)
    if "symbol" not in result:
        result.insert(0, "symbol", symbol)
    return result


def _concat_logs(frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)
