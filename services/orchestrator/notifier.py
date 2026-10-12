"""Tell the user how their call is going.

- When a call starts, a timer is armed for the expected call window
  (``EXPECTED_CALL_WINDOW_S``). If the call is still running when it fires, the
  user gets one "still on the line" update.
- When the call ends, the user gets exactly one message: a plain-language
  summary on success, or which way it failed (no answer / busy / hung up /
  couldn't connect). The task moves to ``completed`` or ``failed``.

Outcomes arrive from two places: the voice agent reports a finished task with
its summary, and the telephony vendor reports how the line closed. Whichever
arrives first wins; a success already reported is never overwritten by the
vendor's later "completed" (which on its own only means the line closed).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Protocol

import messages
from models import TaskStatus
from telephony import CallOutcome

logger = logging.getLogger("siyona.orchestrator.notifier")

DEFAULT_EXPECTED_CALL_WINDOW_S = 180.0


class Sender(Protocol):
    async def send_text(self, to: str, body: str) -> None: ...


def expected_window_from_env() -> float:
    return float(os.environ.get("EXPECTED_CALL_WINDOW_S", DEFAULT_EXPECTED_CALL_WINDOW_S))


def outcome_message(outcome: CallOutcome, callee: str | None, summary: str | None) -> str:
    if outcome is CallOutcome.COMPLETED:
        return messages.call_succeeded(summary, callee)
    if outcome is CallOutcome.NO_ANSWER:
        return messages.call_no_answer(callee)
    if outcome is CallOutcome.BUSY:
        return messages.call_busy(callee)
    if outcome is CallOutcome.HUNG_UP:
        return messages.call_hung_up(callee)
    return messages.CALL_FAILED


class StatusNotifier:
    def __init__(self, sender: Sender, expected_window_s: float | None = None) -> None:
        self._sender = sender
        self.expected_window_s = (
            expected_window_s if expected_window_s is not None else expected_window_from_env()
        )
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._finished: set[str] = set()

    async def _send(self, to: str, text: str) -> None:
        try:
            await self._sender.send_text(to, text)
        except Exception:
            logger.exception("could not send status update")

    def call_started(self, task: Any) -> None:
        """Arm the "still on the line" timer for a call that has just been placed."""
        self._cancel_timer(task.id)
        self._timers[task.id] = asyncio.create_task(self._interim(task))

    async def _interim(self, task: Any) -> None:
        await asyncio.sleep(self.expected_window_s)
        if task.id in self._finished or task.status is not TaskStatus.IN_PROGRESS:
            return
        logger.info("task %s is past its expected call window", task.id)
        await self._send(task.user_phone, messages.still_on_the_line(task.callee_name))

    def _cancel_timer(self, task_id: str) -> None:
        timer = self._timers.pop(task_id, None)
        if timer is not None and not timer.done():
            timer.cancel()

    async def call_finished(
        self, task: Any, outcome: CallOutcome, summary: str | None = None
    ) -> bool:
        """Report the end of the call once. Returns False if it was already reported."""
        if task.id in self._finished:
            return False
        self._finished.add(task.id)
        self._cancel_timer(task.id)
        task.status = (
            TaskStatus.COMPLETED if outcome is CallOutcome.COMPLETED else TaskStatus.FAILED
        )
        logger.info("task %s finished: %s", task.id, outcome)
        await self._send(task.user_phone, outcome_message(outcome, task.callee_name, summary))
        return True

    async def aclose(self) -> None:
        timers = list(self._timers.values())
        self._timers.clear()
        for timer in timers:
            timer.cancel()
        for timer in timers:
            with contextlib.suppress(asyncio.CancelledError):
                await timer
