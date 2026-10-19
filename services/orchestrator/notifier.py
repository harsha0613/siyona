"""Tell the user how their call is going.

- When a call starts, a timer is armed for the expected call window
  (``EXPECTED_CALL_WINDOW_S``). If the call is still running when it fires, the
  user gets one "still on the line" update.
- When the call ends, the user gets exactly one message: a plain-language
  summary on success, or which way it failed (no answer / busy / hung up /
  couldn't connect). The task moves to ``completed`` or ``failed``. A completed
  call's transcript follows the summary as a second message.
- A task with ``max_attempts`` > 1 (a call to a store the assistant found) is
  redialled after ``CALL_RETRY_DELAY_S`` when the line is busy, nobody answers,
  or the call fails, through the same dialer, so the business, number and task
  stay the same. The user is told each time; only the last failure is final.

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
from collections.abc import Sequence
from typing import Any, Protocol

import messages
from models import TaskStatus
from telephony import CallOutcome

logger = logging.getLogger("siyona.orchestrator.notifier")

DEFAULT_EXPECTED_CALL_WINDOW_S = 180.0
DEFAULT_RETRY_DELAY_S = 120.0
RETRYABLE_OUTCOMES = frozenset({CallOutcome.BUSY, CallOutcome.NO_ANSWER, CallOutcome.FAILED})


class Sender(Protocol):
    async def send_text(self, to: str, body: str) -> None: ...


class Redialer(Protocol):
    async def dial(self, task: Any) -> None: ...


def retry_delay_from_env() -> float:
    return float(os.environ.get("CALL_RETRY_DELAY_S", DEFAULT_RETRY_DELAY_S))


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
    def __init__(
        self,
        sender: Sender,
        expected_window_s: float | None = None,
        redialer: Redialer | None = None,
        retry_delay_s: float | None = None,
    ) -> None:
        self._sender = sender
        self.expected_window_s = (
            expected_window_s if expected_window_s is not None else expected_window_from_env()
        )
        self._redialer = redialer
        self.retry_delay_s = retry_delay_s if retry_delay_s is not None else retry_delay_from_env()
        self._timers: dict[str, asyncio.Task[None]] = {}
        self._retries: dict[str, asyncio.Task[None]] = {}
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

    def _should_retry(self, task: Any, outcome: CallOutcome) -> bool:
        return (
            self._redialer is not None
            and outcome in RETRYABLE_OUTCOMES
            and getattr(task, "attempt", 1) < getattr(task, "max_attempts", 1)
        )

    async def call_finished(
        self,
        task: Any,
        outcome: CallOutcome,
        summary: str | None = None,
        transcript: Sequence[tuple[str, str]] | None = None,
    ) -> bool:
        """Report the end of the call once. Returns False if it was already reported."""
        if task.id in self._finished or task.id in self._retries:
            return False
        self._cancel_timer(task.id)
        if self._should_retry(task, outcome):
            logger.info("task %s attempt %d: %s; redialling", task.id, task.attempt, outcome)
            minutes = max(1, round(self.retry_delay_s / 60))
            await self._send(
                task.user_phone,
                messages.retrying(
                    str(outcome), task.callee_name, task.attempt, task.max_attempts, minutes
                ),
            )
            self._retries[task.id] = asyncio.create_task(self._redial(task))
            return True
        self._finished.add(task.id)
        task.status = (
            TaskStatus.COMPLETED if outcome is CallOutcome.COMPLETED else TaskStatus.FAILED
        )
        logger.info("task %s finished: %s", task.id, outcome)
        await self._send(task.user_phone, outcome_message(outcome, task.callee_name, summary))
        if outcome is CallOutcome.COMPLETED and transcript:
            await self._send(
                task.user_phone, messages.call_transcript(list(transcript), task.callee_name)
            )
        return True

    async def _redial(self, task: Any) -> None:
        await asyncio.sleep(self.retry_delay_s)
        task.attempt += 1
        assert self._redialer is not None
        try:
            await self._redialer.dial(task)
        except Exception:
            logger.exception("redial failed for task %s", task.id)
            self._retries.pop(task.id, None)
            await self.call_finished(task, CallOutcome.FAILED)
            return
        self._retries.pop(task.id, None)
        self.call_started(task)

    async def aclose(self) -> None:
        timers = [*self._timers.values(), *self._retries.values()]
        self._timers.clear()
        self._retries.clear()
        for timer in timers:
            timer.cancel()
        for timer in timers:
            with contextlib.suppress(asyncio.CancelledError):
                await timer
