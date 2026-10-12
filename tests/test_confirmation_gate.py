"""The confirmation gate: who/what/when summary, and no call without a YES."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import messages
import pytest
import tasks
from confirmation import ConfirmationGate, Reply, parse_reply
from dispatcher import CallDispatcher
from fastapi.testclient import TestClient
from intake import ASAP, parse_instruction
from models import TaskStatus
from telephony import MockTelephony
from webhook import SIGNATURE_HEADER, compute_signature, create_app
from whatsapp import MockWhatsApp

USER = "14155550100"
CALLEE = "+14155550123"
INSTRUCTION = "Call Bella Vista on +1 415 555 0123 and book a table for 4 tonight at 7pm"
SECRET = "dev-secret-not-for-production"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture(autouse=True)
def _empty_store():
    tasks.TASK_STORE.clear()
    yield
    tasks.TASK_STORE.clear()


@pytest.fixture()
def harness():
    outbox = MockWhatsApp()
    phone = MockTelephony()
    clock = Clock()
    gate = ConfirmationGate(outbox, CallDispatcher(phone, "k"), timeout_s=300, clock=clock)
    return gate, outbox, phone, clock


def _new_task(gate: ConfirmationGate, text: str = INSTRUCTION) -> tasks.Task:
    task = tasks.create_task(USER, text)
    asyncio.run(gate.open(task))
    return task


# --- intake: who / what / when ------------------------------------------------


def test_intake_extracts_who_what_when() -> None:
    spec = parse_instruction(INSTRUCTION, "US")
    assert spec.numbers == [CALLEE]
    assert spec.callee_name == "Bella Vista"
    assert spec.when == "tonight at 7pm"
    assert "book a table for 4" in spec.goal
    assert "555" not in spec.goal


def test_intake_defaults_when_to_asap() -> None:
    assert parse_instruction("Call +1 415 555 0123 about my order", "US").when == ASAP


def test_intake_only_uses_numbers_the_user_wrote() -> None:
    assert parse_instruction("Call the pizza place near me", "US").numbers == []


@pytest.mark.parametrize("text", ["yes", "Yes!", "ok", "go ahead", "YES PLEASE"])
def test_parse_reply_yes(text: str) -> None:
    assert parse_reply(text) is Reply.YES


@pytest.mark.parametrize("text", ["no", "No.", "cancel", "nope"])
def test_parse_reply_no(text: str) -> None:
    assert parse_reply(text) is Reply.NO


def test_parse_reply_other() -> None:
    assert parse_reply("actually call the dentist instead") is None


# --- the gate -----------------------------------------------------------------


def test_summary_is_sent_before_any_dialling(harness) -> None:
    gate, outbox, phone, _ = harness
    task = _new_task(gate)

    assert task.status is TaskStatus.AWAITING_CONFIRMATION
    assert phone.placed == []
    [summary] = outbox.texts_to(USER)
    assert "Who: Bella Vista on +1 415-555-0123" in summary
    assert "What: " in summary and "book a table for 4" in summary
    assert "When: tonight at 7pm" in summary


def test_confirm_sets_in_progress_and_dials(harness) -> None:
    gate, outbox, phone, _ = harness
    task = _new_task(gate)

    assert asyncio.run(gate.handle_reply(USER, "yes")) is True
    assert task.status is TaskStatus.IN_PROGRESS
    assert phone.placed == [(CALLEE, task.call_id)]
    assert task.provider_call_sid
    assert outbox.texts_to(USER)[-1] == messages.CALLING_NOW


def test_decline_cancels_without_calling(harness) -> None:
    gate, outbox, phone, _ = harness
    task = _new_task(gate)

    assert asyncio.run(gate.handle_reply(USER, "no")) is True
    assert task.status is TaskStatus.CANCELLED
    assert phone.placed == []
    assert outbox.texts_to(USER)[-1] == messages.DECLINED


def test_timeout_cancels_without_calling(harness) -> None:
    gate, outbox, phone, clock = harness
    task = _new_task(gate)

    clock.advance(299)
    assert asyncio.run(gate.expire_overdue()) == 0
    assert task.status is TaskStatus.AWAITING_CONFIRMATION

    clock.advance(1)
    assert asyncio.run(gate.expire_overdue()) == 1
    assert task.status is TaskStatus.CANCELLED
    assert phone.placed == []
    assert outbox.texts_to(USER)[-1] == messages.confirmation_expired(5)


def test_yes_after_the_deadline_does_not_call(harness) -> None:
    gate, outbox, phone, clock = harness
    task = _new_task(gate)
    clock.advance(301)

    assert asyncio.run(gate.handle_reply(USER, "yes")) is True
    assert task.status is TaskStatus.CANCELLED
    assert phone.placed == []
    assert outbox.texts_to(USER)[-1] == messages.confirmation_expired(5)


def test_new_instruction_supersedes_a_pending_one_without_calling(harness) -> None:
    gate, _, phone, _ = harness
    task = _new_task(gate)

    assert asyncio.run(gate.handle_reply(USER, "Actually call the dentist")) is False
    assert task.status is TaskStatus.CANCELLED
    assert phone.placed == []


def test_missing_number_is_asked_for_then_confirmed(harness) -> None:
    gate, outbox, phone, _ = harness
    task = _new_task(gate, "Call Bella Vista and book a table for 2 tomorrow")

    assert task.status is TaskStatus.NEEDS_USER_INPUT
    assert outbox.texts_to(USER)[-1] == messages.ask_number("Bella Vista")

    assert asyncio.run(gate.handle_reply(USER, "it's +1 415 555 0123")) is True
    assert task.status is TaskStatus.AWAITING_CONFIRMATION
    assert "When: tomorrow" in outbox.texts_to(USER)[-1]
    assert phone.placed == []


def test_dial_failure_marks_the_task_failed(harness) -> None:
    gate, outbox, _, _ = harness

    class Refusing:
        async def dial(self, task) -> None:
            raise RuntimeError("vendor down")

    gate._dialer = Refusing()
    task = _new_task(gate)
    asyncio.run(gate.handle_reply(USER, "yes"))

    assert task.status is TaskStatus.FAILED
    assert outbox.texts_to(USER)[-1] == messages.CALL_NOT_STARTED


def test_expiry_loop_stops_cleanly(harness) -> None:
    gate, *_ = harness

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(gate.run_expiry_loop(stop, interval_s=0.01))
        await asyncio.sleep(0.03)
        stop.set()
        await asyncio.wait_for(loop, timeout=1)

    asyncio.run(run())


# --- end to end through the webhook -------------------------------------------


def _envelope(text: str) -> dict:
    message = {"from": USER, "id": "wamid.X", "type": "text", "text": {"body": text}}
    return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}


def _post(client: TestClient, text: str):
    body = json.dumps(_envelope(text)).encode()
    return client.post(
        "/webhook/whatsapp",
        content=body,
        headers={SIGNATURE_HEADER: compute_signature(body, SECRET)},
    )


def test_webhook_confirm_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    outbox, phone = MockWhatsApp(), MockTelephony()
    client = TestClient(create_app(whatsapp=outbox, dialer=CallDispatcher(phone, "k")))

    assert _post(client, INSTRUCTION).json()["status"] == "accepted"
    assert phone.placed == []
    assert _post(client, "YES").json()["status"] == "reply_handled"

    [task] = tasks.TASK_STORE.values()
    assert task.status is TaskStatus.IN_PROGRESS
    assert phone.placed == [(CALLEE, task.call_id)]


def test_webhook_decline_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    outbox, phone = MockWhatsApp(), MockTelephony()
    client = TestClient(create_app(whatsapp=outbox, dialer=CallDispatcher(phone, "k")))

    _post(client, INSTRUCTION)
    _post(client, "no")

    [task] = tasks.TASK_STORE.values()
    assert task.status is TaskStatus.CANCELLED
    assert phone.placed == []
