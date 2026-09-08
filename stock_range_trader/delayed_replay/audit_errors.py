"""Stable reason codes; never copy paths, payloads or external exceptions."""


class AuditError(ValueError):
    """Base error for the local audit contract."""


class InvalidEvent(AuditError):
    pass


class IdempotencyConflict(AuditError):
    pass


class HeadConflict(AuditError):
    pass


class IntegrityError(AuditError):
    pass


class IdentityMismatch(AuditError):
    pass


class StoreBusy(AuditError):
    pass
