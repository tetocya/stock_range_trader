"""Strict policies, pure sizing boundaries and no daily-bar inputs."""

import inspect
from dataclasses import asdict
from decimal import localcontext

import pytest
from delayed_replay_account_helpers import Harness, evidence, policy, request

from delayed_replay.account_models import OrderRequest
from delayed_replay.account_policy import AccountPolicy
from delayed_replay.audit_errors import InvalidEvent
from delayed_replay.execution import ExecutionOpenEvidence
from delayed_replay.sizing import size_buy


@pytest.mark.parametrize(
    "price,fee,shares",
    [
        ("300", "0", 0),
        ("100", "0", 200),
        ("200", "0", 100),
        ("200", "0.001", 0),
        ("100", "0.001", 100),
    ],
)
def test_cost_inclusive_lot_boundaries(price, fee, shares):
    result = size_buy(price, "200000", "200000", policy(commission_rate=fee))
    assert result.shares == shares
    assert result.shares % 100 == 0
    assert result.frozen_budget == "20000"


@pytest.mark.parametrize(
    "name",
    [
        "max_position_pct",
        "commission_rate",
        "slippage_pct",
        "reservation_buffer_pct",
        "price_quantum",
        "money_quantum",
    ],
)
@pytest.mark.parametrize("value", [True, 0.1, -1, "NaN", "Infinity", "-0.1", None])
def test_invalid_exact_policy_values_rejected(name, value):
    with pytest.raises((ValueError, TypeError)):
        policy(**{name: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"max_positions": True},
        {"max_positions": 0},
        {"lot_size": 1},
        {"commission_rate": "1"},
        {"slippage_pct": "1"},
        {"max_position_pct": "1.1"},
        {"priority_mode": None},
        {"proceeds_mode": "reuse_same_open"},
        {"fill_mode": "partial"},
        {"position_mode": "pyramid"},
        {"expiry_mode": "carry"},
        {"amount_rounding": "unknown"},
        {"purpose": "formal_oos"},
    ],
)
def test_unsupported_modes_are_not_defaulted(changes):
    with pytest.raises((ValueError, TypeError)):
        policy(**changes)


def test_all_policy_fields_are_explicit_and_precision_is_local():
    values = asdict(policy())
    for name in values:
        missing = dict(values)
        missing.pop(name)
        with pytest.raises((InvalidEvent, TypeError)):
            AccountPolicy.from_dict(missing)
    expected = size_buy("99.99", "200000", "200000", policy(commission_rate="0.001"))
    with localcontext() as context:
        context.prec = 6
        assert (
            size_buy("99.99", "200000", "200000", policy(commission_rate="0.001"))
            == expected
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"reference_price": True},
        {"reference_price": 100.0},
        {"reference_price": "-1"},
        {"lot_size": True},
        {"lot_size": 1},
        {"requested_shares": 100},
        {"side": "SHORT"},
        {"side": "SELL", "requested_shares": True},
        {"side": "SELL", "requested_shares": 50},
        {"side": "SELL", "requested_shares": -100},
    ],
)
def test_order_quantities_types_and_long_only_rejected(changes):
    with pytest.raises((InvalidEvent, TypeError)):
        request(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "yfinance"},
        {"provider": "unknown"},
        {"provider_price_basis": "unknown"},
        {"purpose": "live"},
        {"open_price": 100.0},
        {"open_price": True},
        {"open_price": "NaN"},
        {"split_ratio": "2"},
        {"snapshot_hash": "bad"},
    ],
)
def test_execution_evidence_fail_closed(changes):
    with pytest.raises((InvalidEvent, TypeError)):
        evidence("A", "2024-01-03", **changes)


def test_no_daily_bar_fields_in_sizing_or_order_contract(tmp_path):
    assert set(inspect.signature(size_buy).parameters) == {
        "reference_price",
        "decision_equity",
        "available_cash",
        "policy",
    }
    first, second = Harness(tmp_path / "one.sqlite3"), Harness(tmp_path / "two.sqlite3")
    try:
        daily1 = {
            "open": "100",
            "high": "110",
            "low": "90",
            "close": "105",
            "volume": 1000,
        }
        daily2 = {"open": "999", "high": "1000", "low": "1", "close": "2", "volume": 0}
        for h, daily in ((first, daily1), (second, daily2)):
            h.submit(request())  # Today's daily fields are not inputs at all.
            with pytest.raises((InvalidEvent, TypeError)):
                OrderRequest.from_dict(dict(asdict(request()), **daily))
            with pytest.raises((InvalidEvent, TypeError)):
                ExecutionOpenEvidence.from_dict(
                    dict(asdict(evidence("A", "2024-01-03")), volume=daily["volume"])
                )
        assert first.state == second.state
        first.execute("2024-01-03", evidence("A", "2024-01-03", "99"))
        second.execute("2024-01-03", evidence("A", "2024-01-03", "101"))
        assert (
            first.state["orders"]["buy-A"]["shares"]
            == second.state["orders"]["buy-A"]["shares"]
            == 200
        )
        assert (
            first.state["orders"]["buy-A"]["frozen_budget"]
            == second.state["orders"]["buy-A"]["frozen_budget"]
        )
    finally:
        first.close()
        second.close()
