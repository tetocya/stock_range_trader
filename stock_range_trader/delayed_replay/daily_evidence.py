"""Offline J-Quants reported values; never auction/fill authorization."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from data.price_policy import provider_price_basis

from .serialization import JsonObject, decimal_text, require_hash, time_text
from .validation import ReplayContractError

DAILY_SCHEMA = "provider-daily-open-7a-1"
JQUANTS_RESPONSE_SCHEMA = "jquants-v2-equities-bars-daily"
DAILY_FIELDS = (
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


class PriceEvidenceKind(StrEnum):
    PROVIDER_DAILY_OPEN = "provider_daily_open"
    AUCTION_EXECUTION = "auction_execution_evidence"
    SIMULATED_FILL = "simulated_fill"


class UnsupportedDailyExecution(ReplayContractError):
    """A reported daily opening value is not an executable opening auction."""


def numeric(value):
    if value is None:
        return None
    if type(value) not in (int, float, str):
        raise ReplayContractError("invalid_numeric_type")
    try:
        if len(str(value)) > 128:
            raise ValueError
        number = Decimal(str(value))
        if not number.is_finite() or abs(number) > Decimal("1e30"):
            raise ValueError
        rendered = format(number, "f")
        if "." in rendered:
            rendered = rendered.rstrip("0").rstrip(".")
        return decimal_text("0" if number == 0 else rendered)
    except (InvalidOperation, ValueError, OverflowError):
        raise ReplayContractError("invalid_numeric_value") from None


def quality(values, ex_right):
    for i, value in enumerate(values):
        if value is not None and (
            Decimal(value) < 0 or (i not in (4, 9) and Decimal(value) == 0)
        ):
            return "invalid", "invalid_ohlcv_or_factor"
    if any(v is None for v in values):
        return "missing", "null_reported_value_cause_unknown"
    numbers = list(map(Decimal, values))
    for o, h, low, c, volume in (numbers[:5], numbers[5:10]):
        if (
            min(o, h, low, c) <= 0
            or volume < 0
            or not low <= min(o, c) <= max(o, c) <= h
        ):
            return "invalid", "invalid_ohlcv"
    if numbers[10] <= 0:
        return "invalid", "invalid_adjustment_factor"
    if numbers[10] != 1 or ex_right is not None:
        return "unsupported", "corporate_action_requires_review"
    if numbers[4] == 0:
        return "no_trade_reported", "zero_daily_volume_post_session_only"
    return "reported", "daily_value_not_order_executability"


@dataclass(frozen=True, slots=True)
class DailyOpenObservation:
    payload: JsonObject
    payload_sha256: str

    def __post_init__(self):
        p = self.payload.to_dict()
        if self.payload.sha256 != self.payload_sha256 or set(p) != {
            "schema",
            "kind",
            "provider",
            "provider_price_basis",
            "response_schema",
            "symbol",
            "session",
            "snapshot_hash",
            "values",
            "ex_right",
            "first_observed_at",
            "fetched_at",
            "trade_at",
            "quality",
            "reason",
        }:
            raise ReplayContractError("daily_observation_integrity")
        if (
            p["schema"] != DAILY_SCHEMA
            or p["kind"] != PriceEvidenceKind.PROVIDER_DAILY_OPEN
            or p["provider"] != "jquants"
            or p["provider_price_basis"] != provider_price_basis("jquants")
            or p["response_schema"] != JQUANTS_RESPONSE_SCHEMA
        ):
            raise ReplayContractError("daily_observation_contract")
        require_hash(p["snapshot_hash"])
        if (
            not isinstance(p["symbol"], str)
            or not p["symbol"]
            or date.fromisoformat(p["session"]).isoformat() != p["session"]
        ):
            raise ReplayContractError("daily_observation_identity")
        from .replay_calendar import JST
        from .serialization import parse_time

        if (
            parse_time(p["first_observed_at"]) > parse_time(p["fetched_at"])
            or p["trade_at"] is not None
            or parse_time(p["first_observed_at"]).astimezone(JST).date()
            < date.fromisoformat(p["session"])
        ):
            raise ReplayContractError("daily_trade_time_not_known")
        if (
            type(p["values"]) is not list
            or len(p["values"]) != len(DAILY_FIELDS)
            or any(
                v is not None and (type(v) is not str or numeric(v) != v)
                for v in p["values"]
            )
        ):
            raise ReplayContractError("daily_values_contract")
        if p["ex_right"] not in (None, "1", "2", "3"):
            raise ReplayContractError("unknown_corporate_action_code")
        if (p["quality"], p["reason"]) != quality(p["values"], p["ex_right"]):
            raise ReplayContractError("daily_quality_mismatch")

    def to_dict(self):
        return self.payload.to_dict()

    def require_execution(self):
        raise UnsupportedDailyExecution(
            "daily_open_is_not_auction_evidence;proxy_model_unapproved"
        )


def adapt_daily_response(
    response,
    *,
    provider,
    basis,
    response_schema,
    symbol,
    session: date,
    first_observed_at: datetime,
    fetched_at: datetime,
    snapshot_hash,
):
    """One exact requested record, no code padding, null-to-zero or API calls.

    snapshot_hash binds the canonical artificial/raw response before adaptation.
    Real captured responses must be normalized with response_payload() likewise.
    """
    if (
        provider != "jquants"
        or basis != provider_price_basis("jquants")
        or response_schema != JQUANTS_RESPONSE_SCHEMA
    ):
        raise ReplayContractError("unsupported_provider_basis_schema")
    if (
        type(session) is not date
        or type(response) is not dict
        or not {"Date", "Code", *DAILY_FIELDS} <= set(response)
    ):
        raise ReplayContractError("daily_response_schema")
    if (
        response["Code"] != symbol
        or response["Date"] != session.isoformat()
        or type(symbol) is not str
        or not symbol
    ):
        raise ReplayContractError("daily_symbol_session_mismatch")
    source = response_payload(response)
    if source.sha256 != snapshot_hash:
        raise ReplayContractError("daily_response_hash_mismatch")
    values = [numeric(response[k]) for k in DAILY_FIELDS]
    ex_right = response.get("ExRT")
    if ex_right not in (None, "1", "2", "3"):
        raise ReplayContractError("unknown_corporate_action_code")
    status, reason = quality(values, ex_right)
    p = JsonObject.from_value(
        dict(
            schema=DAILY_SCHEMA,
            kind=PriceEvidenceKind.PROVIDER_DAILY_OPEN.value,
            provider=provider,
            provider_price_basis=basis,
            response_schema=response_schema,
            symbol=symbol,
            session=session.isoformat(),
            snapshot_hash=snapshot_hash,
            values=values,
            ex_right=ex_right,
            first_observed_at=time_text(first_observed_at),
            fetched_at=time_text(fetched_at),
            trade_at=None,
            quality=status,
            reason=reason,
        )
    )
    return DailyOpenObservation(p, p.sha256)


def response_payload(response):
    """Canonical value capture retaining all response fields; numerics as strings."""

    def encode(v):
        if type(v) is dict:
            return {k: encode(x) for k, x in v.items()}
        if type(v) is list:
            return [encode(x) for x in v]
        if type(v) in (int, float):
            return numeric(v)
        return v

    return JsonObject.from_value(encode(response))
