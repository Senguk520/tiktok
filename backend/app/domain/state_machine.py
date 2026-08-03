"""Pure write-operation state transition rules."""

from __future__ import annotations

from app.domain.enums import OperationKind, WriteState


class InvalidTransition(ValueError):
    """Raised when an operation attempts to skip a required durable state."""


_BASE_TRANSITIONS: dict[WriteState, frozenset[WriteState]] = {
    WriteState.VALIDATING: frozenset({WriteState.QUEUED, WriteState.FAILED}),
    WriteState.QUEUED: frozenset({WriteState.SUBMITTED, WriteState.FAILED}),
    WriteState.SUBMITTED: frozenset(
        {WriteState.AUDITING, WriteState.ACTIVE, WriteState.FAILED, WriteState.MANUAL_REVIEW}
    ),
    WriteState.AUDITING: frozenset(
        {WriteState.ACTIVE, WriteState.FAILED, WriteState.MANUAL_REVIEW}
    ),
    WriteState.MANUAL_REVIEW: frozenset({WriteState.ACTIVE, WriteState.FAILED}),
    WriteState.ACTIVE: frozenset(),
    WriteState.FAILED: frozenset(),
}


def transition_write_state(
    current: WriteState,
    target: WriteState,
    *,
    operation: OperationKind,
) -> WriteState:
    """Return the target only if the domain permits that exact transition.

    Content-changing operations cannot jump directly from SUBMITTED to ACTIVE;
    they need platform audit evidence. Price, inventory, deactivate and delete
    operations may become ACTIVE immediately after a confirmed response.
    """

    if target not in _BASE_TRANSITIONS[current]:
        raise InvalidTransition(f"illegal write transition: {current} -> {target}")
    if (
        current is WriteState.SUBMITTED
        and target is WriteState.ACTIVE
        and operation.requires_audit
    ):
        raise InvalidTransition("content-changing operation requires AUDITING evidence")
    return target