"""Tests for SMA, ATR, and ADX."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from indicators import adx, atr, sma, true_range


def test_sma_matches_known_values() -> None:
    close = pd.Series([1.0, 2.0, 3.0, 4.0])

    result = sma(close, period=3)

    np.testing.assert_allclose(result, [np.nan, np.nan, 2.0, 3.0], equal_nan=True)
    assert result.name == "sma"


def test_true_range_matches_known_values() -> None:
    high = pd.Series([10.0, 13.0, 12.0])
    low = pd.Series([8.0, 9.0, 10.0])
    close = pd.Series([9.0, 12.0, 11.0])

    result = true_range(high, low, close)

    np.testing.assert_allclose(result, [2.0, 4.0, 2.0])


def test_simple_atr_matches_known_values() -> None:
    high = pd.Series([10.0, 13.0, 12.0])
    low = pd.Series([8.0, 9.0, 10.0])
    close = pd.Series([9.0, 12.0, 11.0])

    result = atr(high, low, close, period=2, method="simple")

    np.testing.assert_allclose(result, [np.nan, 3.0, 3.0], equal_nan=True)


def test_wilder_atr_matches_known_values() -> None:
    high = pd.Series([10.0, 13.0, 12.0])
    low = pd.Series([8.0, 9.0, 10.0])
    close = pd.Series([9.0, 12.0, 11.0])

    result = atr(high, low, close, period=2)

    np.testing.assert_allclose(result, [np.nan, 3.0, 2.5], equal_nan=True)
    assert result.name == "atr"


def test_adx_matches_manually_calculated_wilder_values() -> None:
    high = pd.Series([10.0, 12.0, 11.0, 13.0, 12.0])
    low = pd.Series([8.0, 9.0, 8.0, 10.0, 9.0])
    close = pd.Series([9.0, 11.0, 9.0, 12.0, 10.0])

    result = adx(high, low, close, period=2)

    expected = [np.nan, np.nan, np.nan, 33.3333333333, 16.6666666667]
    np.testing.assert_allclose(result, expected, rtol=1e-10, equal_nan=True)
    assert result.name == "adx"


def test_adx_is_zero_for_constant_prices_after_warmup() -> None:
    prices = pd.Series([10.0] * 5)

    result = adx(prices, prices, prices, period=2)

    np.testing.assert_allclose(
        result, [np.nan, np.nan, np.nan, 0.0, 0.0], equal_nan=True
    )


def test_adx_rejects_period_one_under_talib_convention() -> None:
    prices = pd.Series([10.0, 11.0, 12.0])

    with pytest.raises(ValueError, match="at least 2"):
        adx(prices, prices, prices, period=1)


def test_adx_period_14_matches_talib_initialization_golden_fixture() -> None:
    # Expected values were frozen from the TA-Lib TA_ADX initialization
    # convention with unstable period 0: period-1 initial DM/TR observations,
    # followed by period Wilder-smoothed DX observations. TA-Lib is not a
    # runtime or test dependency.
    close = pd.Series(
        [
            100,
            102,
            101,
            103,
            105,
            104,
            106,
            107,
            105,
            104,
            106,
            108,
            109,
            107,
            106,
            105,
            107,
            109,
            110,
            108,
            107,
            109,
            111,
            112,
            110,
            108,
            109,
            111,
            113,
            112,
            110,
            111,
            113,
            115,
            114,
            116,
            117,
            115,
            114,
            116,
        ],
        dtype=float,
    )
    high = close + pd.Series(
        [
            2,
            2,
            1,
            2,
            2,
            1,
            2,
            1,
            2,
            1,
            2,
            2,
            1,
            2,
            1,
            2,
            2,
            1,
            2,
            2,
            1,
            2,
            1,
            2,
            2,
            1,
            2,
            2,
            1,
            2,
            2,
            1,
            2,
            1,
            2,
            2,
            1,
            2,
            1,
            2,
        ],
        dtype=float,
    )
    low = close - pd.Series(
        [
            1,
            2,
            2,
            1,
            2,
            2,
            1,
            2,
            1,
            2,
            2,
            1,
            2,
            1,
            2,
            1,
            2,
            2,
            1,
            2,
            2,
            1,
            2,
            1,
            2,
            2,
            1,
            2,
            2,
            1,
            2,
            2,
            1,
            2,
            1,
            2,
            2,
            1,
            2,
            1,
        ],
        dtype=float,
    )

    result = adx(high, low, close, period=14)

    expected = [
        29.161722887271416,
        29.130019267911045,
        29.100580192790698,
        27.91541844644127,
        26.814911110545374,
        26.58942163762085,
        26.61858342787382,
        26.64566223310872,
        27.151170898817277,
        27.62057180268951,
        27.572074070899045,
        26.634204700046478,
        26.60200792550527,
    ]
    assert result.first_valid_index() == 27
    assert result.iloc[:27].isna().all()
    np.testing.assert_allclose(result.iloc[27:], expected, rtol=1e-12)


@pytest.mark.parametrize("function", [sma, atr, adx])
def test_period_must_be_a_positive_integer(function: object) -> None:
    values = pd.Series([1.0, 2.0, 3.0])
    arguments = (values,) if function is sma else (values, values, values)

    with pytest.raises(ValueError, match="positive integer"):
        function(*arguments, period=0)  # type: ignore[operator]


def test_ohlc_inputs_must_share_an_index() -> None:
    high = pd.Series([2.0, 3.0], index=[0, 1])
    low = pd.Series([1.0, 2.0], index=[0, 2])
    close = pd.Series([1.5, 2.5], index=[0, 1])

    with pytest.raises(ValueError, match="identical indexes"):
        atr(high, low, close, period=2)


def test_future_changes_do_not_alter_past_indicator_values() -> None:
    index = pd.date_range("2025-01-01", periods=50, freq="D")
    close = pd.Series(100.0 + np.sin(np.arange(50) / 2), index=index)
    high = close + 2.0
    low = close - 2.0

    original = pd.DataFrame(
        {
            "sma": sma(close, period=5),
            "atr": atr(high, low, close, period=5),
            "adx": adx(high, low, close, period=5),
        }
    )

    changed_close = close.copy()
    changed_close.iloc[40:] += 1_000.0
    changed_high = high.copy()
    changed_high.iloc[40:] += 1_000.0
    changed_low = low.copy()
    changed_low.iloc[40:] += 1_000.0
    changed = pd.DataFrame(
        {
            "sma": sma(changed_close, period=5),
            "atr": atr(changed_high, changed_low, changed_close, period=5),
            "adx": adx(changed_high, changed_low, changed_close, period=5),
        }
    )

    pd.testing.assert_frame_equal(original.iloc[:40], changed.iloc[:40])


def test_unknown_atr_method_is_rejected() -> None:
    values = pd.Series([1.0, 2.0, 3.0])

    with pytest.raises(ValueError, match="wilder.*simple"):
        atr(values + 1.0, values - 1.0, values, period=2, method="centered")  # type: ignore[arg-type]
