"""Point-in-time domestic common-stock universe construction."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from .symbol_mapping import SymbolMappingError, jquants_to_yfinance

DOMESTIC_COMMON_STOCK_PRODUCT_CODE = "011"
TSE_TARGET_MARKETS: frozenset[str] = frozenset({"0111", "0112", "0113"})
UNIVERSE_COLUMNS: tuple[str, ...] = (
    "as_of_date",
    "jquants_code",
    "company_name",
    "market_segment_code",
    "market_segment_name",
    "sector17_code",
    "sector17_name",
    "sector33_code",
    "sector33_name",
    "product_category",
    "yfinance_ticker",
    "universe_included",
    "exclusion_reason",
)
REQUIRED_MASTER_COLUMNS: frozenset[str] = frozenset(
    {
        "Code",
        "CoName",
        "Mkt",
        "MktNm",
        "S17",
        "S17Nm",
        "S33",
        "S33Nm",
        "ProdCat",
    }
)


def build_japanese_equity_universe(
    master: pd.DataFrame, *, as_of_date: date
) -> pd.DataFrame:
    """Classify every master row using official product and market codes."""

    if not isinstance(master, pd.DataFrame):
        raise TypeError("J-Quants master must be a pandas DataFrame")
    missing = sorted(REQUIRED_MASTER_COLUMNS - set(master.columns))
    if missing:
        raise ValueError("J-Quants master missing columns: " + ", ".join(missing))
    if master.empty:
        return pd.DataFrame(columns=UNIVERSE_COLUMNS)
    if any(not isinstance(code, str) for code in master["Code"]):
        raise ValueError("J-Quants Code must remain string typed")
    if "Date" in master:
        snapshot_dates = pd.to_datetime(master["Date"], errors="coerce")
        if snapshot_dates.isna().any() or set(snapshot_dates.dt.date) != {as_of_date}:
            raise ValueError(
                "J-Quants master Date must exactly match as_of_date; "
                "a current universe cannot be relabeled as a historical snapshot"
            )

    records: list[dict[str, object]] = []
    for _, row in master.iterrows():
        code = row["Code"].strip().upper()
        market_code = str(row["Mkt"])
        product_code = str(row["ProdCat"])
        ticker = ""
        exclusion = ""
        if product_code != DOMESTIC_COMMON_STOCK_PRODUCT_CODE:
            exclusion = f"non_domestic_common_stock:{product_code}"
        elif market_code not in TSE_TARGET_MARKETS:
            exclusion = f"outside_prime_standard_growth:{market_code}"
        else:
            try:
                ticker = jquants_to_yfinance(code)
            except SymbolMappingError as error:
                exclusion = f"unresolved_symbol:{error}"
        records.append(
            {
                "as_of_date": pd.Timestamp(as_of_date),
                "jquants_code": code,
                "company_name": str(row["CoName"]),
                "market_segment_code": market_code,
                "market_segment_name": str(row["MktNm"]),
                "sector17_code": str(row["S17"]),
                "sector17_name": str(row["S17Nm"]),
                "sector33_code": str(row["S33"]),
                "sector33_name": str(row["S33Nm"]),
                "product_category": product_code,
                "yfinance_ticker": ticker,
                "universe_included": not exclusion,
                "exclusion_reason": exclusion,
            }
        )
    return pd.DataFrame.from_records(records, columns=UNIVERSE_COLUMNS).sort_values(
        "jquants_code", kind="stable", ignore_index=True
    )


def unresolved_symbols(universe: pd.DataFrame) -> pd.DataFrame:
    """Return all rows whose J-Quants code could not be mapped safely."""

    _validate_universe_columns(universe)
    mask = universe["exclusion_reason"].astype(str).str.startswith("unresolved_symbol:")
    return universe.loc[
        mask, ["as_of_date", "jquants_code", "company_name", "exclusion_reason"]
    ].reset_index(drop=True)


def save_universe_snapshot(
    universe: pd.DataFrame,
    path: str | Path,
    *,
    unresolved_path: str | Path | None = None,
) -> None:
    """Save the complete included/excluded snapshot and unresolved mapping rows."""

    _validate_universe_columns(universe)
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    universe.to_csv(output, index=False)
    if unresolved_path is not None:
        unresolved_output = Path(unresolved_path)
        unresolved_output.parent.mkdir(parents=True, exist_ok=True)
        unresolved_symbols(universe).to_csv(unresolved_output, index=False)


def _validate_universe_columns(universe: pd.DataFrame) -> None:
    if not isinstance(universe, pd.DataFrame):
        raise TypeError("universe must be a pandas DataFrame")
    missing = sorted(set(UNIVERSE_COLUMNS) - set(universe.columns))
    if missing:
        raise ValueError("universe missing columns: " + ", ".join(missing))
