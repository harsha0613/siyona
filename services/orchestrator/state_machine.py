"""Call state machine and task fan-out for the orchestrator.

One task may require several calls (a shortlist of restaurants, a redial after
a busy signal). The orchestrator owns which calls exist, what state each is in,
and how their results consolidate into a single outcome for the user.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

MAX_ATTEMPTS_PER_NUMBER = 3


class CallState(StrEnum):
    QUEUED = "queued"
    DIALING = "dialing"
    IN_IVR = "in_ivr"
    CONNECTED = "connected"
    COMPLETED = "completed"
    FAILED = "failed"


#: Legal transitions. Anything not listed here raises InvalidTransition.
TRANSITIONS: dict[CallState, frozenset[CallState]] = {
    CallState.QUEUED: frozenset({CallState.DIALING, CallState.FAILED}),
    CallState.DIALING: frozenset({CallState.IN_IVR, CallState.CONNECTED, CallState.FAILED}),
    CallState.IN_IVR: frozenset({CallState.CONNECTED, CallState.FAILED}),
    CallState.CONNECTED: frozenset({CallState.COMPLETED, CallState.FAILED}),
    CallState.COMPLETED: frozenset(),
    CallState.FAILED: frozenset({CallState.QUEUED}),  # retry
}

TERMINAL_STATES = frozenset({CallState.COMPLETED})


class InvalidTransition(RuntimeError):
    """Raised when a call is moved into a state it cannot reach."""


@dataclass
class CallAttempt:
    """A single outbound call tracked by the orchestrator."""

    callee_number: str
    state: CallState = CallState.QUEUED
    attempt: int = 1
    summary: str | None = None
    history: list[CallState] = field(default_factory=lambda: [CallState.QUEUED])

    def transition(self, target: CallState) -> CallState:
        if target not in TRANSITIONS[self.state]:
            raise InvalidTransition(f"{self.state.value} -> {target.value} is not allowed")
        if self.state is CallState.FAILED and target is CallState.QUEUED:
            self.attempt += 1
        self.state = target
        self.history.append(target)
        return self.state

    @property
    def can_retry(self) -> bool:
        return self.state is CallState.FAILED and self.attempt < MAX_ATTEMPTS_PER_NUMBER


@dataclass
class TaskRun:
    """Fan-out of one task across a shortlist of numbers."""

    task_id: str
    instruction: str
    calls: list[CallAttempt] = field(default_factory=list)

    @classmethod
    def fan_out(cls, task_id: str, instruction: str, numbers: Iterable[str]) -> TaskRun:
        return cls(
            task_id=task_id,
            instruction=instruction,
            calls=[CallAttempt(callee_number=number) for number in numbers],
        )

    @property
    def settled(self) -> bool:
        """True once no call can make further progress."""
        return all(
            call.state in TERMINAL_STATES or (call.state is CallState.FAILED and not call.can_retry)
            for call in self.calls
        )

    def consolidate(self) -> tuple[bool, str]:
        """Collapse every call result into ``(succeeded, summary)``."""
        successes = [call for call in self.calls if call.state is CallState.COMPLETED]
        if successes:
            first = successes[0]
            detail = first.summary or "task completed"
            return True, f"{detail} (via {first.callee_number})"
        attempted = len(self.calls)
        return False, f"No one could help after {attempted} call(s); nothing was booked."
