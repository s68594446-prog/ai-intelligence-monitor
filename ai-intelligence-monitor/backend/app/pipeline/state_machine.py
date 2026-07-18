from sqlalchemy.orm import Session

from app.core.enums import PipelineState
from app.models import StateTransitionAudit


class InvalidStateTransition(ValueError):
    pass


ALLOWED_TRANSITIONS: dict[PipelineState, set[PipelineState]] = {
    PipelineState.COLLECTED: {PipelineState.NORMALIZED, PipelineState.FAILED},
    PipelineState.NORMALIZED: {
        PipelineState.DEDUPED,
        PipelineState.DUPLICATE,
        PipelineState.OUT_OF_SCOPE,
        PipelineState.FAILED,
    },
    PipelineState.DEDUPED: {PipelineState.TRIAGED, PipelineState.FAILED},
    PipelineState.TRIAGED: {
        PipelineState.ANALYZED,
        PipelineState.OUT_OF_SCOPE,
        PipelineState.UNVERIFIED,
        PipelineState.CONFLICTED,
        PipelineState.INSUFFICIENT_EVIDENCE,
        PipelineState.REJECTED,
        PipelineState.FAILED,
    },
    PipelineState.ANALYZED: {
        PipelineState.RED_TEAMED,
        PipelineState.ADJUDICATED,
        PipelineState.UNVERIFIED,
        PipelineState.CONFLICTED,
        PipelineState.INSUFFICIENT_EVIDENCE,
        PipelineState.REJECTED,
        PipelineState.FAILED,
    },
    PipelineState.RED_TEAMED: {
        PipelineState.ADJUDICATED,
        PipelineState.UNVERIFIED,
        PipelineState.CONFLICTED,
        PipelineState.INSUFFICIENT_EVIDENCE,
        PipelineState.REJECTED,
        PipelineState.FAILED,
    },
    PipelineState.ADJUDICATED: {PipelineState.RENDERED, PipelineState.FAILED},
    PipelineState.RENDERED: {PipelineState.OPS_CHECKED, PipelineState.FAILED},
}


def is_allowed(from_state: PipelineState, to_state: PipelineState) -> bool:
    return to_state in ALLOWED_TRANSITIONS.get(from_state, set())


def transition(
    session: Session,
    *,
    run_id: str,
    entity,
    to_state: PipelineState,
    reason: str,
) -> None:
    from_state = PipelineState(entity.pipeline_state)
    if not is_allowed(from_state, to_state):
        raise InvalidStateTransition(f"illegal transition {from_state.value} -> {to_state.value}")
    entity.pipeline_state = to_state
    session.add(
        StateTransitionAudit(
            run_id=run_id,
            entity_type=entity.__class__.__name__,
            entity_id=entity.id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
        )
    )
    session.flush()
