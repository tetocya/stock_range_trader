"""Point-in-time universe and symbol-mapping tests."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from phase2_helpers import master_frame

from universe import (
    SymbolMappingError,
    build_japanese_equity_universe,
    jquants_to_yfinance,
    save_universe_snapshot,
    unresolved_symbols,
    yfinance_to_jquants,
)


def test_verified_numeric_and_alphanumeric_codes_round_trip() -> None:
    assert jquants_to_yfinance("72030") == "7203.T"
    assert jquants_to_yfinance("130A0") == "130A.T"
    assert yfinance_to_jquants("7203.T") == "72030"
    assert yfinance_to_jquants("130a.t") == "130A0"


@pytest.mark.parametrize("invalid", [72030, "72031", "7203", "A2030", "7203.X"])
def test_symbol_mapping_never_guesses(invalid) -> None:
    with pytest.raises(SymbolMappingError):
        jquants_to_yfinance(invalid)


def test_universe_includes_only_official_common_stock_and_target_market_codes() -> None:
    master = master_frame()
    excluded = master.iloc[[1]].copy()
    excluded.loc[:, "Code"] = "13430"
    excluded.loc[:, "CoName"] = "ETF"
    excluded.loc[:, "ProdCat"] = "012"
    outside = master.iloc[[1]].copy()
    outside.loc[:, "Code"] = "99990"
    outside.loc[:, "CoName"] = "PRO issue"
    outside.loc[:, "Mkt"] = "0109"
    master = pd.concat([master, excluded, outside], ignore_index=True)

    universe = build_japanese_equity_universe(master, as_of_date=date(2026, 8, 31))

    included = universe.loc[universe["universe_included"]]
    assert list(included["jquants_code"]) == ["130A0", "72030"]
    assert list(included["yfinance_ticker"]) == ["130A.T", "7203.T"]
    reasons = dict(
        zip(
            universe["jquants_code"],
            universe["exclusion_reason"],
            strict=True,
        )
    )
    assert reasons["13430"] == "non_domestic_common_stock:012"
    assert reasons["99990"] == "outside_prime_standard_growth:0109"
    assert set(universe["as_of_date"].dt.date) == {date(2026, 8, 31)}
    assert all(isinstance(value, str) for value in universe["jquants_code"])


def test_current_master_cannot_be_relabelled_as_historical() -> None:
    with pytest.raises(ValueError, match="cannot be relabeled"):
        build_japanese_equity_universe(master_frame(), as_of_date=date(2021, 8, 31))


def test_numeric_master_code_is_rejected() -> None:
    master = master_frame()
    master["Code"] = master["Code"].astype(object)
    master.loc[0, "Code"] = 13000

    with pytest.raises(ValueError, match="remain string"):
        build_japanese_equity_universe(master, as_of_date=date(2026, 8, 31))


def test_unresolved_symbol_is_recorded_and_saved(tmp_path) -> None:
    master = master_frame()
    master.loc[0, "Code"] = "12AB0"
    universe = build_japanese_equity_universe(master, as_of_date=date(2026, 8, 31))

    unresolved = unresolved_symbols(universe)
    assert list(unresolved["jquants_code"]) == ["12AB0"]
    assert unresolved.loc[0, "exclusion_reason"].startswith("unresolved_symbol:")

    snapshot_path = tmp_path / "universe.csv"
    unresolved_path = tmp_path / "unresolved_symbols.csv"
    save_universe_snapshot(universe, snapshot_path, unresolved_path=unresolved_path)
    assert snapshot_path.is_file()
    assert list(pd.read_csv(unresolved_path, dtype=str)["jquants_code"]) == ["12AB0"]
