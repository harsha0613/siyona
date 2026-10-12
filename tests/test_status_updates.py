"""Status updates on WhatsApp: outcome when a call ends, interim update if it runs long."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import messages
import pytest
import tasks
from dispatcher import CallDispatcher
from fastapi.testclient import TestClient
from models import TaskStatus
from notifier import StatusNotifier, outcome_message
from telephony import (
    SIGNATURE_HEADER,
    CallOutcome,
    MockTelephony,
    TelephonyConfig,
    TwilioTelephony,
    compute_signature,
)
from webhook import SIGNATURE_HEADER as WA_SIGNATURE_HEADER
from webhook import compute_signature as wa_signature
from webhook import create_app
from whatsapp import MockWhatsApp

USER = "14155550100"
CALLEE_NAME = "Bella Vista"
WA_SECRET = "dev-secret-not-for-production"
AUTH_TOKEN = "test-auth-token"
GATEWAY = "https://gateway.example.test"


@pytest.fixture(autouse=True)
def _empty_store():
    tasks.TASK_STORE.clear()
    yield
    tasks.TASK_STORE.clear()


def _task(**overrides) -> SimpleNamespace:
    fields = {
        "id": "t1",
        "user_phone": USER,
        "callee_name": CALLEE_NAME,
        "status": TaskStatus.IN_PROGRESS,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


# --- messages -----------------------------------------------------------------


def test_success_message_is_the_plain_language_summary() -> None:
    text = outcome_message(CallOutcome.COMPLETED, CALLEE_NAME, "Table for 4 booked at 7pm.")
    assert text == "✅ Done. Table for 4 booked at 7pm."


def test_success_without_summary_still_says_what_happened() -> None:
    assert CALLEE_NAME in outcome_message(CallOutcome.COMPLETED, CALLEE_NAME, None)


@pytest.mark.parametrize(
    ("outcome", "phrase"),
    [
        (CallOutcome.NO_ANSWER, "No one answered"),
        (CallOutcome.BUSY, "line was busy"),
        (CallOutcome.HUNG_UP, "hung up"),
        (CallOutcome.FAILED, "didn't go through"),
    ],
)
def test_each_failure_type_has_its_own_message(outcome: CallOutcome, phrase: str) -> None:
    text = outcome_message(outcome, CALLEE_NAME, None)
    assert phrase in text
    assert "Nothing was done" in text


# --- notifier -----------------------------------------------------------------


def test_success_notifies_once_and_completes_the_task() -> None:
    outbox = MockWhatsApp()
    notifier = StatusNotifier(outbox, expected_window_s=60)
    task = _task()

    async def run() -> tuple[bool, bool]:
        notifier.call_started(task)
        first = await notifier.call_finished(task, CallOutcome.COMPLETED, "Booked for 7pm.")
        # The vendor's "line closed" after a reported success must not override it.
        second = await notifier.call_finished(task, CallOutcome.HUNG_UP)
        await notifier.aclose()
        return first, second

    assert asyncio.run(run()) == (True, False)
    assert task.status is TaskStatus.COMPLETED
    assert outbox.texts_to(USER) == ["✅ Done. Booked for 7pm."]


@pytest.mark.parametrize(
    "outcome", [CallOutcome.NO_ANSWER, CallOutcome.BUSY, CallOutcome.HUNG_UP, CallOutcome.FAILED]
)
def test_failure_notifies_with_the_failure_type(outcome: CallOutcome) -> None:
    outbox = MockWhatsApp()
    notifier = StatusNotifier(outbox, expected_window_s=60)
    task = _task()
    asyncio.run(notifier.call_finished(task, outcome))

    assert task.status is TaskStatus.FAILED
    assert outbox.texts_to(USER) == [outcome_message(outcome, CALLEE_NAME, None)]


def test_interim_update_when_the_call_runs_past_the_window() -> None:
    outbox = MockWhatsApp()
    notifier = StatusNotifier(outbox, expected_window_s=0.02)
    task = _task()

    async def run() -> None:
        notifier.call_started(task)
        await asyncio.sleep(0.08)
        await notifier.call_finished(task, CallOutcome.COMPLETED, "Done.")

    asyncio.run(run())
    assert outbox.texts_to(USER) == [messages.still_on_the_line(CALLEE_NAME), "✅ Done. Done."]


def test_no_interim_update_when_the_call_ends_in_time() -> None:
    outbox = MockWhatsApp()
    notifier = StatusNotifier(outbox, expected_window_s=0.05)
    task = _task()

    async def run() -> None:
        notifier.call_started(task)
        await notifier.call_finished(task, CallOutcome.BUSY)
        await asyncio.sleep(0.1)

    asyncio.run(run())
    assert outbox.texts_to(USER) == [messages.call_busy(CALLEE_NAME)]


def test_whatsapp_outage_does_not_break_the_notifier() -> None:
    class Broken:
        async def send_text(self, to: str, body: str) -> None:
            raise RuntimeError("down")

    task = _task()
    assert asyncio.run(StatusNotifier(Broken(), 60).call_finished(task, CallOutcome.BUSY))
    assert task.status is TaskStatus.FAILED


# --- vendor status callbacks --------------------------------------------------


def _twilio() -> TwilioTelephony:
    return TwilioTelephony(
        TelephonyConfig(
            account_sid="AC-test", auth_token=AUTH_TOKEN, status_callback_base_url=GATEWAY
        )
    )


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("busy", CallOutcome.BUSY),
        ("no-answer", CallOutcome.NO_ANSWER),
        ("completed", CallOutcome.HUNG_UP),
        ("failed", CallOutcome.FAILED),
        ("canceled", CallOutcome.FAILED),
        ("ringing", None),
        ("in-progress", None),
    ],
)
def test_twilio_statuses_map_to_outcomes(status: str, outcome: CallOutcome | None) -> None:
    url = f"{GATEWAY}/telephony/status?call_id=c1"
    params = {"CallSid": "CA1", "CallStatus": status}
    headers = {SIGNATURE_HEADER: compute_signature(AUTH_TOKEN, url, params)}
    progress = _twilio().parse_status_callback(url, params, headers)
    assert (progress.call_id, progress.outcome) == ("c1", outcome)


def test_mock_telephony_refuses_status_callbacks() -> None:
    with pytest.raises(Exception, match="mock"):
        MockTelephony().parse_status_callback("u", {}, {})


# --- end to end ---------------------------------------------------------------


def _wa_post(client: TestClient, text: str) -> None:
    message = {"from": USER, "id": "wamid.X", "type": "text", "text": {"body": text}}
    body = json.dumps({"entry": [{"changes": [{"value": {"messages": [message]}}]}]}).encode()
    client.post(
        "/webhook/whatsapp",
        content=body,
        headers={WA_SIGNATURE_HEADER: wa_signature(body, WA_SECRET)},
    )


def _status_post(client: TestClient, call_id: str, status: str, token: str = AUTH_TOKEN):
    query = urlencode({"call_id": call_id})
    params = {"CallSid": "CA1", "CallStatus": status}
    signature = compute_signature(token, f"{GATEWAY}/telephony/status?{query}", params)
    return client.post(
        f"/telephony/status?{query}",
        content=urlencode(params),
        headers={
            SIGNATURE_HEADER: signature,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


@pytest.fixture()
def live_app(monkeypatch: pytest.MonkeyPatch):
    """Gateway with a Twilio adapter whose HTTP calls are faked."""
    import httpx

    monkeypatch.setenv("WHATSAPP_APP_SECRET", WA_SECRET)
    monkeypatch.setenv("GATEWAY_PUBLIC_URL", GATEWAY)
    phone = TwilioTelephony(
        TelephonyConfig(
            account_sid="AC-test", auth_token=AUTH_TOKEN, status_callback_base_url=GATEWAY
        ),
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(201, json={"sid": "CA1"}))
        ),
    )
    outbox = MockWhatsApp()
    app = create_app(whatsapp=outbox, dialer=CallDispatcher(phone, "k"), expected_call_window_s=60)
    return app, outbox


def _place_confirmed_call(client: TestClient) -> tasks.Task:
    _wa_post(client, "Call Bella Vista on +1 415 555 0123 and book a table for 4 at 7pm")
    _wa_post(client, "yes")
    [task] = tasks.TASK_STORE.values()
    assert task.status is TaskStatus.IN_PROGRESS
    return task


def test_busy_callback_sends_a_failure_update(live_app) -> None:
    app, outbox = live_app
    with TestClient(app) as client:
        task = _place_confirmed_call(client)
        assert _status_post(client, task.call_id, "ringing").json() == {"status": "ok"}
        assert _status_post(client, task.call_id, "busy").json() == {"status": "ok"}

    assert task.status is TaskStatus.FAILED
    assert outbox.texts_to(USER)[-1] == messages.call_busy(CALLEE_NAME)


def test_forged_callback_is_rejected(live_app) -> None:
    app, outbox = live_app
    with TestClient(app) as client:
        task = _place_confirmed_call(client)
        before = list(outbox.outbox)
        response = _status_post(client, task.call_id, "busy", token="wrong-token")

    assert response.status_code == 403
    assert task.status is TaskStatus.IN_PROGRESS
    assert outbox.outbox == before


def test_callback_for_an_unknown_call_is_ignored(live_app) -> None:
    app, _ = live_app
    with TestClient(app) as client:
        assert _status_post(client, "nope", "busy").json() == {"status": "unknown_call"}
