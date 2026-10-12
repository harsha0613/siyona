"""The confirmation gate: nothing is dialled until the user says yes.

Every new task is parsed into who / what / when and that summary is sent back
on WhatsApp. The task then waits in ``AWAITING_CONFIRMATION``:

- YES → status ``IN_PROGRESS`` and the call is handed to the dialer.
- NO → ``CANCELLED``; no call.
- No answer within ``CONFIRMATION_TIMEOUT_S`` → ``CANCELLED``; no call. A YES
  that arrives after the deadline does not revive it.
- Anything else while a confirmation is pending is treated as a new
  instruction, and the pending one is cancelled without a call.

If the instruction has no phone number, the task waits in ``NEEDS_USER_INPUT``
for the user to send one, and then goes through the same gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

import messages
from intake import parse_instruction
from models import TaskStatus
from phone import PhoneNumberError, display_number, find_numbers, region_of, whatsapp_to_e164
from tasks import TASK_STORE, Task, latest_for, utcnow
from whatsapp import WhatsAppSender

logger = logging.getLogger("siyona.gateway.confirmation")

DEFAULT_TIMEOUT_S = 600.0


class Reply(StrEnum):
    YES = "yes"
    NO = "no"


_YES = {
    "YES", "Y", "YEP", "YEAH", "OK", "OKAY", "SURE", "CONFIRM", "GO", "GO AHEAD",
    "CALL", "YES CALL", "YES PLEASE", "DO IT",
}  # fmt: skip
_NO = {"NO", "N", "NOPE", "DONT", "DO NOT", "NO THANKS", "CANCEL", "STOP"}


def parse_reply(text: str) -> Reply | None:
    normalised = " ".join(re.sub(r"[^\w\s]", "", text).upper().split())
    if normalised in _YES:
        return Reply.YES
    if normalised in _NO:
        return Reply.NO
    return None


class Dialer(Protocol):
    async def dial(self, task: Task) -> None:
        """Place the call for a confirmed task. Raises if the call could not start."""
        ...


class CallWatcher(Protocol):
    def call_started(self, task: Task) -> None: ...


def timeout_from_env() -> float:
    return float(os.environ.get("CONFIRMATION_TIMEOUT_S", DEFAULT_TIMEOUT_S))


def _region(user_phone: str) -> str | None:
    try:
        return region_of(whatsapp_to_e164(user_phone))
    except PhoneNumberError:
        return None


class ConfirmationGate:
    def __init__(
        self,
        sender: WhatsAppSender,
        dialer: Dialer,
        timeout_s: float | None = None,
        clock: Callable[[], datetime] = utcnow,
        watcher: CallWatcher | None = None,
    ) -> None:
        self._sender = sender
        self._dialer = dialer
        self._watcher = watcher
        self.timeout = timedelta(seconds=timeout_s if timeout_s is not None else timeout_from_env())
        self._clock = clock

    async def _send(self, to: str, text: str) -> None:
        try:
            await self._sender.send_text(to, text)
        except Exception:
            logger.exception("could not send WhatsApp message")

    async def open(self, task: Task) -> None:
        """Parse a new task and ask the user to confirm it."""
        spec = parse_instruction(task.instruction, _region(task.user_phone))
        task.goal, task.callee_name, task.when = spec.goal, spec.callee_name, spec.when
        task.numbers = spec.numbers[:3]
        if spec.missing_number:
            task.status = TaskStatus.NEEDS_USER_INPUT
            await self._send(task.user_phone, messages.ask_number(spec.callee_name))
            return
        await self._ask_to_confirm(task)

    async def _ask_to_confirm(self, task: Task) -> None:
        task.status = TaskStatus.AWAITING_CONFIRMATION
        task.confirm_deadline = self._clock() + self.timeout
        summary = messages.confirm(
            task.goal or task.instruction,
            task.callee_name,
            display_number(task.numbers[0]),
            task.when or "as soon as possible",
        )
        await self._send(task.user_phone, summary)

    def _expired(self, task: Task) -> bool:
        return task.confirm_deadline is not None and self._clock() >= task.confirm_deadline

    async def _expire(self, task: Task) -> None:
        task.status = TaskStatus.CANCELLED
        logger.info("confirmation timed out for task %s", task.id)
        minutes = max(1, round(self.timeout.total_seconds() / 60))
        await self._send(task.user_phone, messages.confirmation_expired(minutes))

    async def handle_reply(self, user_phone: str, text: str) -> bool:
        """Apply ``text`` to the user's pending task. False means it's a new instruction."""
        reply = parse_reply(text)
        pending = latest_for(user_phone, TaskStatus.AWAITING_CONFIRMATION)
        if pending is not None:
            if self._expired(pending):
                await self._expire(pending)
                return reply is not None
            if reply is Reply.NO:
                pending.status = TaskStatus.CANCELLED
                await self._send(user_phone, messages.DECLINED)
                return True
            if reply is Reply.YES:
                await self._start_call(pending)
                return True
            pending.status = TaskStatus.CANCELLED
            logger.info("task %s superseded by a new instruction", pending.id)
            return False

        waiting = latest_for(user_phone, TaskStatus.NEEDS_USER_INPUT)
        if waiting is not None:
            if reply is Reply.NO:
                waiting.status = TaskStatus.CANCELLED
                await self._send(user_phone, messages.DECLINED)
                return True
            numbers = find_numbers(text, _region(user_phone))
            if numbers and len(_strip_numbers(text).split()) <= 6:
                waiting.numbers = numbers[:3]
                await self._ask_to_confirm(waiting)
                return True
            waiting.status = TaskStatus.CANCELLED
        return False

    async def _start_call(self, task: Task) -> None:
        task.status = TaskStatus.IN_PROGRESS
        task.confirm_deadline = None
        try:
            await self._dialer.dial(task)
        except Exception:
            logger.exception("could not start call for task %s", task.id)
            task.status = TaskStatus.FAILED
            await self._send(task.user_phone, messages.CALL_NOT_STARTED)
            return
        await self._send(task.user_phone, messages.CALLING_NOW)
        if self._watcher is not None:
            self._watcher.call_started(task)

    async def expire_overdue(self) -> int:
        """Cancel every task whose confirmation window has passed. Returns how many."""
        overdue = [
            task
            for task in list(TASK_STORE.values())
            if task.status is TaskStatus.AWAITING_CONFIRMATION and self._expired(task)
        ]
        for task in overdue:
            await self._expire(task)
        return len(overdue)

    async def run_expiry_loop(self, stop: asyncio.Event, interval_s: float = 15.0) -> None:
        while not stop.is_set():
            try:
                await self.expire_overdue()
            except Exception:
                logger.exception("confirmation expiry sweep failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)


_NUMBERISH = re.compile(r"\+?\d[\d\s().-]{5,}\d")


def _strip_numbers(text: str) -> str:
    return _NUMBERISH.sub(" ", text)
