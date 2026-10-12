"""I2-1A-2A pure request/role boundary. No reservation execution or authority."""

from dataclasses import dataclass, field
from typing import ClassVar

from . import live_body_contract as c3
from . import live_http_evidence as c2
from .http_contract import HttpContractError
from .live_enrollment_sources import decode_plan

PREFIX = "historical-feasibility-http-reservation-"
RESERVATION_KIND = "i2-http-reserve-v1"


def _require(condition, reason):
    if not condition:
        raise HttpContractError("reservation_boundary_" + reason)


def semantic_request_identity(plan_sha: str, query_id: str, page_index: int) -> str:
    """Same structured identity as Budget v1; no cursor or execution state."""
    c3._sha(plan_sha)
    c3._sha(query_id)
    c3._integer(page_index)
    return c3._hash(
        {
            "domain": "http-reservation-semantic-v1",
            "value": {
                "plan_sha": plan_sha,
                "query_id": query_id,
                "page_index": page_index,
            },
        }
    )


@dataclass(frozen=True)
class HTTPReservationRequestV1(c3.Value):
    """Semantic input only. Store operation ID is supplied separately in future.

    A well-formed request is not historical admission, a slot, or a capability.
    There is deliberately no intent/execute/reserve method.
    """

    schema: ClassVar[str] = PREFIX + "request-v1"
    plan_sha: str
    query_id: str
    page_index: int
    semantic_request_sha: str

    def __post_init__(self):
        super().__post_init__()
        c3._sha(self.semantic_request_sha)
        _require(
            self.semantic_request_sha
            == semantic_request_identity(self.plan_sha, self.query_id, self.page_index),
            "semantic_identity_mismatch",
        )
        self.to_bytes()


@dataclass(frozen=True)
class ReservationRole(c3.Value):
    schema: ClassVar[str] = PREFIX + "role-v1"
    role: str
    journal_kind: str
    event_kind: str


_ROLE_VALUES = (
    ("account-reservation", "account", "slot_reserved"),
    ("plan-reservation", "plan", "attempt_reserved"),
    ("body-reservation", "body", "reserved"),
)
_ROLES = tuple(ReservationRole(*values) for values in _ROLE_VALUES)


@dataclass(frozen=True)
class ReservationBoundaryV1(c3.Value):
    """Fixed future role contract, not a transaction-kernel registration."""

    schema: ClassVar[str] = PREFIX + "boundary-v1"
    kind: str = RESERVATION_KIND
    roles: tuple[ReservationRole, ...] = _ROLES
    reservation_permitted: bool = field(default=False, init=False)
    live_send_permitted: bool = field(default=False, init=False)

    def __post_init__(self):
        super().__post_init__()
        _require(self.kind == RESERVATION_KIND, "kind_mismatch")
        _require(
            tuple((r.role, r.journal_kind, r.event_kind) for r in self.roles)
            == _ROLE_VALUES,
            "roles_mismatch",
        )
        self.to_bytes()


def validate_request(
    request: HTTPReservationRequestV1, snapshot: c3.FixedPlanSnapshot
) -> None:
    """Validate fixed query membership and page bounds, without admitting work.

    Cursor, current heads, attempt number, holder, generation and time must be
    derived from authoritative Store evidence in 2B. None can be supplied here.
    """
    _require(type(request) is HTTPReservationRequestV1, "request_type")
    _require(type(snapshot) is c3.FixedPlanSnapshot, "snapshot_type")
    request = HTTPReservationRequestV1.from_bytes(request.to_bytes())
    plan = decode_plan(snapshot.canonical_plan.encode("utf-8"), snapshot.plan_sha)
    _require(request.plan_sha == plan.sha256, "plan_mismatch")
    _require(
        any(request.query_id == query.query_id for query in plan.queries),
        "query_not_in_plan",
    )
    _require(request.page_index < plan.limits.max_pages_per_query, "page_limit")


def validate_operation_id(operation_id: str) -> None:
    """Existing Store/C2 label syntax only; no uniqueness/receipt claim.

    Operation identity is separate from the stable semantic request identity.
    The future Store transaction must enforce uniqueness and idempotency.
    """
    c2._label(operation_id)
