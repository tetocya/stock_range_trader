"""Stage 1 synthetic contracts only: no API, registration or OOS execution."""

from dataclasses import FrozenInstanceError, asdict, fields, replace
from datetime import UTC, date, datetime, timedelta

import pytest

from data.price_policy import provider_price_basis
from delayed_replay import (
    DelayedReplayConfig,
    PriceObservation,
    PriceSnapshot,
    ReplayClock,
    ReplayDataView,
    ReplayPolicies,
)
from delayed_replay.validation import ReplayContractError


def moment(day: str) -> datetime:
    return datetime.fromisoformat(day).replace(tzinfo=UTC)


def bar(day: str, symbol: str = "TEST-A", price: float = 100.0) -> PriceObservation:
    return PriceObservation(
        symbol=symbol,
        session_date=date.fromisoformat(day),
        market_available_at=moment(day + "T07:00:00"),
        raw_ohlcv=(price, price + 1, price - 1, price, 1000.0),
        adjusted_ohlcv=(price / 2, (price + 1) / 2, (price - 1) / 2, price / 2, 2000.0),
        adjustment_factor=0.5,
        stock_split=0.0,
        dividend=0.0,
    )


def snapshot_values(observations: tuple[PriceObservation, ...]) -> dict:
    return {
        "provider": "jquants",
        "provider_price_basis": provider_price_basis("jquants"),
        "source_artifact_sha256": "a" * 64,
        "data_version": "synthetic-v1",
        "first_observed_at": moment("2024-07-01T01:00:00"),
        "fetched_at": moment("2024-07-01T02:00:00"),
        "provider_published_at": None,
        "publication_time_unknown_reason": "synthetic_not_a_provider_receipt",
        "price_basis_evidence_id": None,
        "observations": observations,
    }


def snapshot(*observations: PriceObservation) -> PriceSnapshot:
    return PriceSnapshot.create(**snapshot_values(observations))


def clock() -> ReplayClock:
    return ReplayClock(moment("2024-04-01T00:00:00"), moment("2024-07-02T00:00:00"))


def explicit_test_policies() -> ReplayPolicies:
    """Every value is a synthetic test choice, never an approved default."""
    values = {
        item.name: "synthetic-test-only:" + item.name for item in fields(ReplayPolicies)
    }
    values.update(
        max_position_pct=0.10,
        max_positions=5,
        lookback_months=3,
        warmup_sessions=80,
        selection_capital=200_000.0,
        commission_rate=0.0,
        slippage_pct=0.001,
        reservation_buffer_pct=0.01,
        finalization_grace_sessions=5,
        market_start=date(2024, 4, 1),
        same_open_sale_proceeds_reuse=False,
    )
    return ReplayPolicies(**values)


def test_default_config_only_contains_confirmed_conditions() -> None:
    config = DelayedReplayConfig()
    assert config.initial_capital == 200_000
    assert config.lot_size == 100
    assert config.account_model == "single_shared"
    assert config.reselection_frequency == "monthly"
    assert all(
        getattr(config.policies, item.name) is None for item in fields(ReplayPolicies)
    )
    with pytest.raises(ReplayContractError, match="unresolved policies"):
        config.policies.require_complete()
    assert not hasattr(config, "registration_timestamp")


def test_explicit_test_configuration_is_complete_but_not_registered() -> None:
    config = DelayedReplayConfig(policies=explicit_test_policies())
    assert config.policies.require_complete() is None
    assert config.policies.unresolved_fields == ()
    assert not hasattr(config, "register")


def test_execution_validation_rejects_draft_but_accepts_explicit_schema() -> None:
    draft = DelayedReplayConfig.from_mapping({"policies": {"max_positions": 2}})
    with pytest.raises(ReplayContractError, match="unresolved policies"):
        draft.validate_for_execution()
    assert draft.policies.max_positions == 2
    assert draft.policies.lookback_months is None
    explicit = DelayedReplayConfig(policies=explicit_test_policies())
    assert explicit.validate_for_execution() is None
    assert not hasattr(explicit, "registration_timestamp")


@pytest.mark.parametrize("name", [item.name for item in fields(ReplayPolicies)])
def test_execution_validation_rejects_every_individual_unresolved_policy(name) -> None:
    policies = replace(explicit_test_policies(), **{name: None})
    draft = DelayedReplayConfig(policies=policies)
    with pytest.raises(ReplayContractError, match=f"unresolved policies: {name}$"):
        draft.validate_for_execution()


def test_mutating_snapshot_source_containers_does_not_change_snapshot_or_hash() -> None:
    source_prices = [100.0, 101.0, 99.0, 100.0, 1000.0]
    source_bars = [replace(bar("2024-03-28"), raw_ohlcv=tuple(source_prices))]
    source_values = snapshot_values(tuple(source_bars))
    original = PriceSnapshot.create(**source_values)
    before = asdict(original)
    digest = original.payload_sha256

    source_prices[0] = 999.0
    source_bars[0] = bar("2024-03-28", price=999.0)
    source_values["observations"] = tuple(source_bars)
    source_values["data_version"] = "changed-input"
    source_values.clear()

    assert asdict(original) == before
    assert original.compute_digest() == original.payload_sha256 == digest


def test_mutating_returned_history_or_export_cannot_change_snapshot_or_hash() -> None:
    original = snapshot(bar("2024-03-28"))
    view = ReplayDataView((original,))
    returned = view.history(clock(), start=date(2024, 1, 1))
    before = asdict(original)
    digest = original.payload_sha256

    with pytest.raises(TypeError):
        returned[0] = bar("2024-03-28", price=999.0)
    with pytest.raises(FrozenInstanceError):
        returned[0].raw_ohlcv = (999.0, 999.0, 999.0, 999.0, 1000.0)
    with pytest.raises(TypeError):
        returned[0].raw_ohlcv[0] = 999.0
    caller_list = list(returned)
    caller_list[0] = bar("2024-03-28", price=999.0)
    exported = asdict(original)
    exported["observations"][0]["symbol"] = "changed-export"
    exported["observations"][0]["raw_ohlcv"] = (999.0,) * 5

    assert asdict(original) == before
    assert original.compute_digest() == original.payload_sha256 == digest
    assert view.history(clock(), start=date(2024, 1, 1)) == returned


@pytest.mark.parametrize(
    "name,value",
    [
        ("provider", "yfinance"),
        ("provider", "unknown"),
        ("execution_mode", "paper"),
        ("execution_mode", "unknown"),
        ("subscription", "paid"),
        ("initial_capital", True),
        ("initial_capital", 200_000.0),
        ("initial_capital", 1_000_000),
        ("account_model", "per_symbol"),
        ("lot_size", 1),
        ("lot_size", True),
        ("lot_size", 100.0),
        ("odd_lots_allowed", True),
        ("odd_lots_allowed", 0),
        ("reselection_frequency", "quarterly"),
        ("policies", {}),
    ],
)
def test_reject_changed_confirmed_contracts(name, value) -> None:
    with pytest.raises(ReplayContractError):
        DelayedReplayConfig(**{name: value})


@pytest.mark.parametrize(
    "values",
    [
        {"unknown": 1},
        {"policies": {"unknown": 1}},
        {"policies": None},
        {"policies": []},
        [],
        None,
    ],
)
def test_mapping_rejects_unknown_or_invalid_structure(values) -> None:
    with pytest.raises(ReplayContractError):
        DelayedReplayConfig.from_mapping(values)


def test_mapping_does_not_mutate_input_or_fill_unresolved_values() -> None:
    values = {"policies": {"max_positions": 2}}
    config = DelayedReplayConfig.from_mapping(values)
    assert config.policies.max_positions == 2
    assert config.policies.max_position_pct is None
    assert values == {"policies": {"max_positions": 2}}
    with pytest.raises(FrozenInstanceError):
        config.policies.max_positions = 5


@pytest.mark.parametrize(
    "name",
    [
        "max_positions",
        "lookback_months",
        "warmup_sessions",
        "finalization_grace_sessions",
    ],
)
@pytest.mark.parametrize("value", [True, 0, -1, 2.0, "3", float("nan")])
def test_reject_invalid_session_and_month_counts(name, value) -> None:
    with pytest.raises(ReplayContractError):
        ReplayPolicies(**{name: value})


@pytest.mark.parametrize(
    "name",
    ["max_position_pct", "commission_rate", "slippage_pct", "reservation_buffer_pct"],
)
@pytest.mark.parametrize("value", [True, -0.1, 1.1, "0.1", float("nan"), float("inf")])
def test_reject_invalid_policy_numbers(name, value) -> None:
    with pytest.raises(ReplayContractError):
        ReplayPolicies(**{name: value})


@pytest.mark.parametrize(
    "values",
    [
        {"max_position_pct": 0},
        {"selection_capital": 0},
        {"selection_capital": False},
        {"market_start": "2024-01-01"},
        {"market_start": moment("2024-01-01")},
        {"same_open_sale_proceeds_reuse": 1},
        {"universe_snapshot_id": ""},
        {"candidate_catalog_id": []},
    ],
)
def test_reject_invalid_other_policies(values) -> None:
    with pytest.raises(ReplayContractError):
        ReplayPolicies(**values)


def test_clock_preserves_calendar_boundary_and_timezone_equivalence() -> None:
    original = ReplayClock(moment("2024-03-31T15:00:00"), moment("2024-07-01"))
    assert original.market_decision_at == datetime.fromisoformat(
        "2024-04-01T00:00:00+09:00"
    )
    moved = original.advance(
        market_decision_at=moment("2024-04-30T15:00:00"),
        replayed_at=moment("2024-08-01"),
    )
    assert moved.market_decision_at == moment("2024-04-30T15:00:00")
    assert original.market_decision_at == moment("2024-03-31T15:00:00")


@pytest.mark.parametrize(
    "value", [datetime(2024, 1, 1), date(2024, 1, 1), "2024-01-01", True, None]
)
def test_clock_rejects_naive_or_invalid_timestamps(value) -> None:
    with pytest.raises(ReplayContractError):
        ReplayClock(value, moment("2024-07-01"))
    with pytest.raises(ReplayContractError):
        ReplayClock(moment("2024-01-01"), value)


def test_clock_rejects_future_market_and_backwards_moves() -> None:
    with pytest.raises(ReplayContractError):
        ReplayClock(moment("2024-07-02"), moment("2024-07-01"))
    for market, replay in [
        (moment("2024-03-01"), clock().replayed_at),
        (clock().market_decision_at, moment("2024-07-01")),
    ]:
        with pytest.raises(ReplayContractError, match="backwards"):
            clock().advance(market_decision_at=market, replayed_at=replay)


def test_snapshot_is_immutable_and_digest_detects_payload_or_digest_tampering() -> None:
    original = snapshot(bar("2024-03-28"))
    assert original.compute_digest() == original.payload_sha256
    for change in (
        {"data_version": "tampered"},
        {"payload_sha256": "0" * 64},
        {"observations": (bar("2024-03-28", price=150),)},
    ):
        with pytest.raises(ReplayContractError, match="SHA-256 mismatch"):
            replace(original, **change)
    with pytest.raises(FrozenInstanceError):
        original.data_version = "changed"
    with pytest.raises(FrozenInstanceError):
        original.observations[0].symbol = "changed"


def test_snapshot_hash_is_input_order_and_timezone_invariant() -> None:
    first, second = bar("2024-03-27"), bar("2024-03-28")
    assert snapshot(first, second) == snapshot(second, first)
    changed_zone = replace(
        first,
        market_available_at=first.market_available_at.astimezone(
            datetime.fromisoformat("2024-03-27T16:00:00+09:00").tzinfo
        ),
    )
    assert snapshot(first).payload_sha256 == snapshot(changed_zone).payload_sha256


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "yfinance"},
        {"provider": "unknown"},
        {"provider_price_basis": "historical_unadjusted"},
        {"source_artifact_sha256": "invalid"},
        {"data_version": ""},
        {"first_observed_at": moment("2024-07-02")},
        {"first_observed_at": datetime(2024, 7, 1)},
        {"publication_time_unknown_reason": None},
        {"provider_published_at": moment("2024-07-02")},
        {"provider_published_at": moment("2024-06-30")},
        {"price_basis_evidence_id": ""},
        {"observations": ()},
        {"observations": (bar("2024-03-28"), bar("2024-03-28"))},
        {"observations": [bar("2024-03-28")]},
    ],
)
def test_snapshot_rejects_invalid_contract(changes) -> None:
    values = snapshot_values((bar("2024-03-28"),))
    values.update(changes)
    with pytest.raises((ReplayContractError, TypeError)):
        PriceSnapshot.create(**values)


def test_known_publication_time_and_receipt_version_are_preserved() -> None:
    values = snapshot_values((bar("2024-03-28"),))
    values.update(
        provider_published_at=moment("2024-06-30"), publication_time_unknown_reason=None
    )
    original = PriceSnapshot.create(**values)
    values["fetched_at"] += timedelta(seconds=1)
    later_receipt = PriceSnapshot.create(**values)
    assert original.payload_sha256 != later_receipt.payload_sha256
    assert original.price_basis_evidence_id is None  # No false verification claim.


@pytest.mark.parametrize(
    "changes",
    [
        {"session_date": moment("2024-03-28")},
        {"symbol": ""},
        {"market_available_at": moment("2024-03-26")},
        {"raw_ohlcv": [100, 101, 99, 100, 1000]},
        {"raw_ohlcv": (True, 101, 99, 100, 1000)},
        {"raw_ohlcv": (100, 99, 98, 100, 1000)},
        {"adjusted_ohlcv": (0, 1, 0, 0, 100)},
        {"adjustment_factor": 0},
        {"dividend": float("nan")},
        {"stock_split": -1},
    ],
)
def test_observation_rejects_invalid_prices_and_types(changes) -> None:
    with pytest.raises(ReplayContractError):
        replace(bar("2024-03-28"), **changes)


def test_history_half_open_start_and_current_session_exclusion() -> None:
    observations = tuple(
        bar(day) for day in ("2024-03-27", "2024-03-28", "2024-03-29", "2024-04-01")
    )
    view = ReplayDataView((snapshot(*observations),))
    assert view.history(clock(), start=date(2024, 3, 28)) == observations[1:3]
    # Full daily bars never leak the current day's close, even late that day.
    late = replace(clock(), market_decision_at=moment("2024-04-01T14:00:00"))
    assert view.history(late, start=date(2024, 3, 28)) == observations[1:3]


def test_market_availability_equality_is_excluded() -> None:
    delayed = replace(bar("2024-03-28"), market_available_at=clock().market_decision_at)
    view = ReplayDataView((snapshot(delayed),))
    assert view.history(clock(), start=date(2024, 3, 1)) == ()


def test_wall_clock_receipt_is_required_and_equality_is_visible() -> None:
    original = snapshot(bar("2024-03-28"))
    view = ReplayDataView((original,))
    before = replace(
        clock(), replayed_at=original.fetched_at - timedelta(microseconds=1)
    )
    assert view.history(before, start=date(2024, 3, 1)) == ()
    at_receipt = replace(clock(), replayed_at=original.fetched_at)
    assert view.history(at_receipt, start=date(2024, 3, 1)) == original.observations


def test_future_prices_symbols_and_corporate_actions_do_not_change_history() -> None:
    past = bar("2024-03-28")
    baseline = ReplayDataView((snapshot(past),)).history(
        clock(), start=date(2024, 1, 1)
    )
    future = replace(bar("2024-04-01", "FUTURE", 10_000), stock_split=10.0)
    for observations in ((future, past), (past, future)):
        actual = ReplayDataView((snapshot(*observations),)).history(
            clock(), start=date(2024, 1, 1)
        )
        assert actual == baseline


def test_future_receipt_of_revision_does_not_replace_pinned_history() -> None:
    original = snapshot(bar("2024-03-28"))
    values = snapshot_values((bar("2024-03-28", price=500),))
    values.update(
        data_version="synthetic-revision",
        first_observed_at=moment("2024-08-01"),
        fetched_at=moment("2024-08-01"),
    )
    revision = PriceSnapshot.create(**values)
    view = ReplayDataView((original, revision))
    assert view.history(clock(), start=date(2024, 1, 1)) == original.observations
    with pytest.raises(ReplayContractError, match="pin one version"):
        view.history(
            replace(clock(), replayed_at=moment("2024-08-02")), start=date(2024, 1, 1)
        )
    assert (
        ReplayDataView((original,)).history(
            replace(clock(), replayed_at=moment("2024-08-02")), start=date(2024, 1, 1)
        )
        == original.observations
    )


def test_distinct_symbol_sessions_leap_year_year_end_and_no_interpolation() -> None:
    observations = (bar("2023-12-29"), bar("2024-02-29"), bar("2024-03-01", "TEST-B"))
    view = ReplayDataView((snapshot(*observations),))
    assert view.history(clock(), start=date(2023, 12, 30)) == observations[1:]
    assert (
        ReplayDataView((snapshot(*reversed(observations)),)).history(
            clock(), start=date(2023, 12, 30)
        )
        == observations[1:]
    )


def test_empty_history_and_invalid_bounds_are_explicit() -> None:
    view = ReplayDataView(())
    assert view.history(clock(), start=date(2024, 4, 1)) == ()
    with pytest.raises(ReplayContractError):
        view.history(clock(), start=date(2024, 4, 2))
    with pytest.raises(ReplayContractError):
        view.history(clock(), start="2024-01-01")
    with pytest.raises(ReplayContractError):
        ReplayDataView([])
