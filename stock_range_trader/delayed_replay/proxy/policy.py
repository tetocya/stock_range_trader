"""No operational defaults; old policy is used only as an arithmetic adapter."""

from dataclasses import dataclass, fields

from data.price_policy import provider_price_basis
from delayed_replay.account_policy import AccountPolicy
from delayed_replay.serialization import JsonObject, digest
from delayed_replay.validation import ReplayContractError

CAPABILITY = dict(
    model_id="daily_open_proxy_v1",
    mode="research_only",
    model_approval="unapproved",
    registration_status="draft_not_registered",
    fill_kind="simulated_fill",
    actual_trade_at=None,
    provenance="generated_synthetic_fixture",
)
ASSUMPTIONS = (
    "fixed_quantity_at_reported_open_plus_slippage",
    "no_queue_market_impact_partial_fill_or_auction_participation",
    "actual_trade_time_and_actual_quantity_not_proven",
    "daily_volume_is_ex_post_contradiction_check_not_open_liquidity",
)


@dataclass(frozen=True)
class DailyOpenProxyPolicy:
    terms: JsonObject | None = None
    rules: JsonObject | None = None

    def require(self):
        if type(self.terms) is not JsonObject or type(self.rules) is not JsonObject:
            raise ReplayContractError("proxy_unresolved_policy")
        expected = dict(
            no_trade="terminal_no_fill",
            volume_excess="reject_instrument_batch",
            missing="wait_without_expiry",
            expiry="explicit_finalization_not_implemented",
            temporary_halt="allow_daily_proxy_without_time_condition",
            time_condition="none",
            volume_unit="execution_shares",
            provider_price_basis=provider_price_basis("jquants"),
            availability="session_phase_not_actual_publication_v1",
            corporate_action="stop_preserve",
            risk="no_new_dd_stop",
            end="mark_without_forced_exit",
        )
        if self.rules.to_dict() != expected:
            raise ReplayContractError("proxy_unsupported_or_missing_rule")
        t = self.terms.to_dict()
        if set(t) != {f.name for f in fields(AccountPolicy)} - {"purpose"} or any(
            v is None for v in t.values()
        ):
            raise ReplayContractError("proxy_unresolved_arithmetic_terms")
        # No price/evidence enters the old execution/replay Gate. Reuse only
        # Decimal rounding, cost and prior-information sizing semantics.
        return AccountPolicy(purpose="synthetic_test", **t)

    @property
    def sha256(self):
        self.require()
        return digest(
            dict(
                terms=self.terms.to_dict(),
                rules=self.rules.to_dict(),
                capability=CAPABILITY,
                assumptions=list(ASSUMPTIONS),
            )
        )

    @property
    def availability_hash(self):
        self.require()
        return digest(dict(availability=self.rules.to_dict()["availability"]))

    def gate(self, mode, source):
        self.require()
        if (
            mode != "offline_synthetic"
            or source.get("generator") != "proxy-fixture-generator-v1"
        ):
            raise ReplayContractError("proxy_real_data_and_formal_forbidden")
