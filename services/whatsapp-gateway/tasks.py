"""In-memory task store for the gateway.

Session state keyed by task id. Replaced by Redis + the API service in R2.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from models import TaskStatus


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Task:
    """A task created from an inbound WhatsApp message."""

    id: str
    user_phone: str
    instruction: str
    status: TaskStatus = TaskStatus.RECEIVED
    created_at: datetime = field(default_factory=utcnow)
    # Filled in by intake before the confirmation gate.
    goal: str | None = None
    callee_name: str | None = None
    numbers: list[str] = field(default_factory=list)
    when: str | None = None
    confirm_deadline: datetime | None = None
    # Filled in once the call is placed.
    call_id: str | None = None
    provider_call_sid: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_phone": self.user_phone,
            "instruction": self.instruction,
            "status": str(self.status),
            "created_at": self.created_at.isoformat(),
        }


TASK_STORE: dict[str, Task] = {}


def create_task(user_phone: str, instruction: str) -> Task:
    """Create and store a Task stub for the orchestrator to fan out."""
    task = Task(id=str(uuid.uuid4()), user_phone=user_phone, instruction=instruction)
    TASK_STORE[task.id] = task
    return task


def find_by_call_id(call_id: str) -> Task | None:
    return next((task for task in TASK_STORE.values() if task.call_id == call_id), None)


def latest_for(user_phone: str, *statuses: TaskStatus) -> Task | None:
    """The user's most recent task in one of ``statuses``."""
    matching = [
        task
        for task in TASK_STORE.values()
        if task.user_phone == user_phone and task.status in statuses
    ]
    return max(matching, key=lambda task: task.created_at, default=None)
