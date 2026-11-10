"""Explicit project and panel state machines. Transitions outside the table are bugs."""

from __future__ import annotations

from enum import StrEnum

from storypanel.errors import invalid_state


class ProjectStatus(StrEnum):
    QUEUED = "queued"
    INTAKE = "intake"
    AWAITING_ANSWERS = "awaiting_answers"
    PLANNING = "planning"
    AWAITING_CAST_APPROVAL = "awaiting_cast_approval"
    SCRIPTING = "scripting"
    AWAITING_SCRIPT_APPROVAL = "awaiting_script_approval"
    ILLUSTRATING = "illustrating"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"  # moderation: terminal, never auto-released
    EXPIRED = "expired"  # an approval gate timed out


S = ProjectStatus
TERMINAL = frozenset({S.COMPLETED, S.PARTIALLY_COMPLETED, S.FAILED, S.CANCELLED, S.BLOCKED, S.EXPIRED})
ACTIVE = frozenset(set(S) - TERMINAL)
_ALWAYS = {S.FAILED, S.CANCELLED, S.BLOCKED}

_TRANSITIONS: dict[ProjectStatus, set[ProjectStatus]] = {
    S.QUEUED: {S.INTAKE},
    S.INTAKE: {S.AWAITING_ANSWERS, S.PLANNING},
    S.AWAITING_ANSWERS: {S.PLANNING, S.EXPIRED},
    S.PLANNING: {S.AWAITING_CAST_APPROVAL},
    S.AWAITING_CAST_APPROVAL: {S.SCRIPTING, S.EXPIRED, S.AWAITING_CAST_APPROVAL},
    S.SCRIPTING: {S.AWAITING_SCRIPT_APPROVAL},
    S.AWAITING_SCRIPT_APPROVAL: {S.ILLUSTRATING, S.EXPIRED},
    S.ILLUSTRATING: {S.COMPLETED, S.PARTIALLY_COMPLETED},
    # A failed/expired run can be resumed from its last durable gate.
    S.FAILED: {
        S.QUEUED,
        S.PLANNING,
        S.SCRIPTING,
        S.ILLUSTRATING,
        S.AWAITING_CAST_APPROVAL,
        S.AWAITING_SCRIPT_APPROVAL,
    },  # fmt: skip
    S.EXPIRED: {S.AWAITING_CAST_APPROVAL, S.AWAITING_SCRIPT_APPROVAL, S.AWAITING_ANSWERS},
    S.PARTIALLY_COMPLETED: {S.COMPLETED, S.ILLUSTRATING},
    S.COMPLETED: {S.PARTIALLY_COMPLETED},
    S.CANCELLED: set(),
    S.BLOCKED: set(),
}


def can_transition(current: ProjectStatus, target: ProjectStatus) -> bool:
    if current == target:
        return True
    if current in (S.CANCELLED, S.BLOCKED):
        return False
    if target in _ALWAYS and current not in TERMINAL:
        return True
    return target in _TRANSITIONS[current]


def assert_transition(current: str, target: ProjectStatus) -> None:
    if not can_transition(ProjectStatus(current), target):
        raise invalid_state(f"Project cannot move from '{current}' to '{target.value}'.")


class PanelStatus(StrEnum):
    PENDING = "pending"
    GENERATING = "generating"
    OK = "ok"
    NEEDS_ATTENTION = "needs_attention"  # quality retries exhausted; image is visible
    BLOCKED = "blocked"  # safety: no image is ever exposed
    FAILED = "failed"  # provider/system failure; can be retried by the user


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
