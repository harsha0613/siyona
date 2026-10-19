"""[A3] Siyona calls the store it found: unit tests for each piece of the call path."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import call_context
import messages
import pytest
import session
import signing
import tasks
from assistant import ASSISTANT_PROMPT, Assistant
from assistant_tools import ToolContext, create_call_request, dialable_number
from call_context import CallContext, call_prompt, opening_line, run_call
from confirmation import ConfirmationGate
from dispatcher import CallDispatcher
from fakes import FakeTelephony, ScriptedModel, call, say, whatsapp_envelope
from fastapi.testclient import TestClient
from maps import MockMaps, PlaceDetails
from models import TaskStatus
from notifier import StatusNotifier
from pipeline import CallPipeline
from telephony import CallOutcome
from vendors.base import TextDelta
from vendors.mock import MockTextToSpeech, RecordingAudioOut, ScriptedSpeechToText
from webhook import SIGNATURE_HEADER, compute_signature, create_app, parse_messages
from whatsapp import MockWhatsApp

USER = "14155550100"
KEY = "test-signing-key"
STORE = PlaceDetails(
    place_id="ChIJpharmacyAAAA0001",
    name="Main Street Pharmacy",
    address="12 Main St, Worcester, MA",
    phone="(508) 555-0142",
    international_phone="+1 508-555-0142",
)
STORE_E164 = "+15085550142"


@pytest.fixture(autouse=True)
def _clean_state():
    for store in (tasks.TASK_STORE, session.SESSIONS, call_context.CALL_CONTEXTS):
        store.clear()
    yield
    for store in (tasks.TASK_STORE, session.SESSIONS, call_context.CALL_CONTEXTS):
        store.clear()


def _gate(outbox: MockWhatsApp, phone: FakeTelephony) -> ConfirmationGate:
    return ConfirmationGate(outbox, CallDispatcher(phone, KEY), timeout_s=300)


def _ctx(gate: ConfirmationGate | None, place: PlaceDetails | None = STORE) -> ToolContext:
    user = session.session_for(USER)
    user.location, user.user_name, user.current_place = (
        "950 Main St, Worcester, MA",
        "Asha K",
        place,
    )
    return ToolContext(user, MockMaps(), gate)


def _request(ctx: ToolContext, **overrides) -> dict:
    arguments = {
        "task": "Ask if they have amoxicillin 500 mg in stock",
        "details": "Customer name: Asha K. Medication: amoxicillin 500 mg. Pickup today.",
        "questions_asked": True,
    }
    arguments.update(overrides)
    return asyncio.run(create_call_request(ctx, **arguments))


# --- the number comes from Place Details, never from the model ------------------


def test_dialable_number_uses_the_international_number() -> None:
    assert dialable_number(STORE, USER) == STORE_E164


def test_dialable_number_falls_back_to_the_local_number_in_the_users_region() -> None:
    local_only = PlaceDetails("ChIJx", "Store", phone="(508) 555-0142")
    assert dialable_number(local_only, USER) == STORE_E164


def test_no_listed_number_means_nothing_to_dial() -> None:
    assert dialable_number(PlaceDetails("ChIJx", "Store"), USER) is None


def test_create_call_request_has_no_phone_argument() -> None:
    from assistant_tools import CREATE_CALL_REQUEST_PARAMETERS

    assert not any("phone" in name for name in CREATE_CALL_REQUEST_PARAMETERS["properties"])


# --- create_call_request -----------------------------------------------------------


def test_call_request_waits_for_the_pre_call_questions() -> None:
    outbox, phone = MockWhatsApp(), FakeTelephony()
    result = _request(_ctx(_gate(outbox, phone)), questions_asked=False)
    assert result["status"] == "ask_first"
    assert tasks.TASK_STORE == {} and outbox.outbox == []


def test_call_request_needs_a_looked_up_place() -> None:
    result = _request(_ctx(_gate(MockWhatsApp(), FakeTelephony()), place=None))
    assert result["status"] == "need_place"
    assert tasks.TASK_STORE == {}


def test_store_without_a_phone_cannot_be_called() -> None:
    result = _request(_ctx(_gate(MockWhatsApp(), FakeTelephony()), PlaceDetails("ChIJx", "Shop")))
    assert result["status"] == "no_phone"
    assert tasks.TASK_STORE == {}


def test_call_request_goes_through_the_confirmation_gate_without_dialling() -> None:
    outbox, phone = MockWhatsApp(), FakeTelephony()
    result = _request(_ctx(_gate(outbox, phone)))

    assert result["status"] == "awaiting_confirmation"
    [task] = tasks.TASK_STORE.values()
    assert task.status is TaskStatus.AWAITING_CONFIRMATION
    assert task.numbers == [STORE_E164]
    assert task.callee_name == "Main Street Pharmacy"
    assert task.place_id == STORE.place_id
    assert (task.user_name, task.user_location) == ("Asha K", "950 Main St, Worcester, MA")
    assert task.max_attempts == 3
    assert phone.placed == []  # nothing is dialled before YES
    [summary] = outbox.texts_to(USER)
    assert summary.startswith("Before I call")
    assert "Main Street Pharmacy on +1 508-555-0142" in summary
    assert "• I'll tell them: Customer name: Asha K." in summary


def test_max_attempts_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_ATTEMPTS_PER_NUMBER", "2")
    _request(_ctx(_gate(MockWhatsApp(), FakeTelephony())))
    [task] = tasks.TASK_STORE.values()
    assert task.max_attempts == 2


def test_yes_dials_the_places_number_with_the_call_context() -> None:
    outbox, phone = MockWhatsApp(), FakeTelephony()
    gate = _gate(outbox, phone)
    _request(_ctx(gate))
    assert asyncio.run(gate.handle_reply(USER, "yes"))

    [task] = tasks.TASK_STORE.values()
    assert phone.placed == [(STORE_E164, task.call_id)]
    context = call_context.context_for(task.call_id)
    assert context is not None
    assert context.business_name == "Main Street Pharmacy"
    assert context.user_name == "Asha K"
    assert context.user_location == "950 Main St, Worcester, MA"
    assert "amoxicillin 500 mg" in (context.details or "")
    assert signing.verify(KEY, call_context.REPORT_TOKEN_PURPOSE, context.report_token) == (
        task.call_id
    )


def test_no_reply_means_no_call() -> None:
    outbox, phone = MockWhatsApp(), FakeTelephony()
    gate = _gate(outbox, phone)
    _request(_ctx(gate))
    asyncio.run(gate.handle_reply(USER, "no"))
    assert phone.placed == []


def test_a_second_call_to_the_same_store_is_a_follow_up() -> None:
    ctx = _ctx(_gate(MockWhatsApp(), FakeTelephony()))
    ctx.session.note_call("first-task", STORE.place_id, STORE.name, "They have it.")
    _request(ctx, task="Reserve it for pickup at 5pm")
    [task] = tasks.TASK_STORE.values()
    assert task.follow_up_of == "first-task"


def test_assistant_sends_only_the_confirmation_summary() -> None:
    outbox, phone = MockWhatsApp(), FakeTelephony()
    gate = _gate(outbox, phone)
    _ctx(gate)
    model = ScriptedModel(
        [
            [
                call(
                    "create_call_request",
                    task="Ask if they have amoxicillin",
                    details="Customer name: Asha K.",
                    questions_asked=True,
                )
            ],
            say("I've sent you the details, reply YES."),
        ]
    )
    reply = asyncio.run(Assistant(model, MockMaps(), gate=gate).reply(USER, "pickup, Asha"))
    assert reply == ""
    assert len(outbox.texts_to(USER)) == 1


def test_prompt_requires_pre_call_questions_and_forbids_typed_numbers() -> None:
    prompt = ASSISTANT_PROMPT.lower()
    assert "ask the 2-3 most important questions" in prompt
    assert "you never choose or type a phone number" in prompt
    assert '"yes, book it"' in prompt


# --- the call itself ---------------------------------------------------------------


CONTEXT = CallContext(
    call_id="c1",
    task_id="t1",
    goal="Ask if they have amoxicillin 500 mg in stock",
    business_name="Main Street Pharmacy",
    details="Medication: amoxicillin 500 mg. Pickup today.",
    user_name="Asha K",
    user_location="950 Main St, Worcester, MA",
)


def test_every_call_opens_with_an_ai_disclosure() -> None:
    assert opening_line(CONTEXT) == (
        "Hi, this is Siyona, an AI assistant calling on behalf of Asha K."
    )
    anonymous = CallContext(call_id="c", task_id="t", goal="g")
    assert "AI assistant" in opening_line(anonymous)


def test_call_prompt_carries_the_user_and_the_task() -> None:
    prompt = call_prompt(CONTEXT)
    for expected in (
        "TASK: Ask if they have amoxicillin 500 mg in stock",
        "CALLING: Main Street Pharmacy",
        "CUSTOMER NAME: Asha K",
        "CUSTOMER LOCATION: 950 Main St, Worcester, MA",
        "DETAILS FROM THE CUSTOMER: Medication: amoxicillin 500 mg.",
        "never invent it",
        "call end_call",
    ):
        assert expected in prompt


def _call_model() -> ScriptedModel:
    return ScriptedModel(
        [
            say("Do you have amoxicillin 500 milligrams in stock?"),
            [
                TextDelta("Great, thank you. Goodbye."),
                call(
                    "end_call",
                    reason="task_complete",
                    outcome_summary="They have amoxicillin 500 mg; ready for pickup by 5pm.",
                ),
            ],
            say("this turn must never be requested"),
        ]
    )


def test_run_call_discloses_talks_and_reports() -> None:
    model = _call_model()
    stt = ScriptedSpeechToText(
        ["Main Street Pharmacy, how can I help", "Yes we have it, ready by five", "Anything else"]
    )
    audio = RecordingAudioOut()
    report = asyncio.run(
        run_call(
            CONTEXT, stt=stt, llm=model, tts=MockTextToSpeech(), audio_out=audio, speculate=False
        )
    )

    assert report.reason == "task_complete"
    assert report.outcome_summary == "They have amoxicillin 500 mg; ready for pickup by 5pm."
    assert report.transcript == (
        ("agent", "Hi, this is Siyona, an AI assistant calling on behalf of Asha K."),
        ("callee", "Main Street Pharmacy, how can I help"),
        ("agent", "Do you have amoxicillin 500 milligrams in stock?"),
        ("callee", "Yes we have it, ready by five"),
        ("agent", "Great, thank you. Goodbye."),
    )
    # end_call stopped the loop: the third callee line was never answered.
    assert len(model.requests) == 2
    first = model.requests[0]
    assert "CUSTOMER NAME: Asha K" in first[0]["content"]
    assert first[1] == {"role": "assistant", "content": opening_line(CONTEXT)}
    assert {t["function"]["name"] for t in model.tools} == {"send_dtmf", "end_call"}
    assert audio.total_bytes > 0


def test_a_call_that_ends_without_end_call_is_reported_as_hung_up() -> None:
    model = ScriptedModel([say("Hello?")])
    report = asyncio.run(
        run_call(
            CONTEXT,
            stt=ScriptedSpeechToText(["Wrong number"]),
            llm=model,
            tts=MockTextToSpeech(),
            audio_out=RecordingAudioOut(),
            speculate=False,
        )
    )
    assert (report.reason, report.outcome_summary) == ("hung_up", None)


def test_pipeline_without_tools_keeps_ignoring_tool_calls(caplog: pytest.LogCaptureFixture) -> None:
    model = ScriptedModel([[call("end_call", reason="task_complete", outcome_summary="x")]])
    pipeline = CallPipeline(
        stt=ScriptedSpeechToText(["hello"]),
        llm=model,
        tts=MockTextToSpeech(),
        audio_out=RecordingAudioOut(),
        speculate=False,
    )
    with caplog.at_level(logging.WARNING):
        asyncio.run(pipeline.run())
    assert pipeline.ended is None
    assert "tools are not enabled" in caplog.text
    assert model.tools == []


# --- retries -------------------------------------------------------------------------


def _places_task(max_attempts: int = 3) -> tasks.Task:
    task = tasks.create_task(USER, "Ask if they have amoxicillin")
    task.goal, task.callee_name, task.numbers = task.instruction, STORE.name, [STORE_E164]
    task.details, task.max_attempts, task.status = "Asha K", max_attempts, TaskStatus.IN_PROGRESS
    return task


def _redial_harness():
    outbox, phone = MockWhatsApp(), FakeTelephony()
    dialer = CallDispatcher(phone, KEY)
    notifier = StatusNotifier(outbox, expected_window_s=60, redialer=dialer, retry_delay_s=0)
    return outbox, phone, dialer, notifier


@pytest.mark.parametrize("outcome", [CallOutcome.BUSY, CallOutcome.NO_ANSWER, CallOutcome.FAILED])
def test_busy_no_answer_or_failed_redials_the_same_number_and_task(outcome: CallOutcome) -> None:
    outbox, phone, dialer, notifier = _redial_harness()
    task = _places_task()

    async def run() -> None:
        await dialer.dial(task)
        first_call = task.call_id
        assert await notifier.call_finished(task, outcome)
        await asyncio.sleep(0.01)
        assert task.call_id != first_call
        await notifier.aclose()

    asyncio.run(run())
    assert phone.placed == [(STORE_E164, phone.placed[0][1]), (STORE_E164, task.call_id)]
    assert task.attempt == 2 and task.status is TaskStatus.IN_PROGRESS
    context = call_context.context_for(task.call_id)
    assert context is not None
    assert (context.task_id, context.business_name, context.details, context.attempt) == (
        task.id,
        STORE.name,
        "Asha K",
        2,
    )
    assert outbox.texts_to(USER) == [
        messages.retrying(str(outcome), STORE.name, 1, 3, 1),
    ]


def test_retries_stop_after_max_attempts_with_the_failure_message() -> None:
    outbox, phone, dialer, notifier = _redial_harness()
    task = _places_task(max_attempts=2)

    async def run() -> None:
        await dialer.dial(task)
        await notifier.call_finished(task, CallOutcome.BUSY)
        await asyncio.sleep(0.01)
        await notifier.call_finished(task, CallOutcome.BUSY)
        await notifier.aclose()

    asyncio.run(run())
    assert len(phone.placed) == 2
    assert task.status is TaskStatus.FAILED
    assert outbox.texts_to(USER)[-1] == messages.call_busy(STORE.name)


def test_a_hang_up_is_not_retried() -> None:
    outbox, phone, dialer, notifier = _redial_harness()
    task = _places_task()

    async def run() -> None:
        await dialer.dial(task)
        await notifier.call_finished(task, CallOutcome.HUNG_UP)
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert len(phone.placed) == 1 and task.status is TaskStatus.FAILED


def test_typed_number_tasks_keep_the_single_attempt() -> None:
    outbox, phone, dialer, notifier = _redial_harness()
    task = _places_task(max_attempts=1)
    asyncio.run(notifier.call_finished(task, CallOutcome.BUSY))
    assert phone.placed == [] and task.status is TaskStatus.FAILED


def test_a_late_duplicate_callback_during_the_redial_wait_is_ignored() -> None:
    outbox, phone, dialer, _ = _redial_harness()
    notifier = StatusNotifier(outbox, 60, redialer=dialer, retry_delay_s=0.05)
    task = _places_task()

    async def run() -> bool:
        await notifier.call_finished(task, CallOutcome.BUSY)
        duplicate = await notifier.call_finished(task, CallOutcome.FAILED)
        await notifier.aclose()
        return duplicate

    assert asyncio.run(run()) is False
    assert len(outbox.texts_to(USER)) == 1


def test_a_redial_that_cannot_start_counts_as_a_failed_attempt() -> None:
    outbox = MockWhatsApp()

    class Down:
        async def dial(self, task) -> None:
            raise RuntimeError("telephony down")

    notifier = StatusNotifier(outbox, 60, redialer=Down(), retry_delay_s=0)
    task = _places_task(max_attempts=2)

    async def run() -> None:
        await notifier.call_finished(task, CallOutcome.BUSY)
        await asyncio.sleep(0.01)

    asyncio.run(run())
    assert task.status is TaskStatus.FAILED
    assert outbox.texts_to(USER)[-1] == messages.CALL_FAILED


def test_completed_call_sends_the_summary_then_the_transcript() -> None:
    outbox = MockWhatsApp()
    task = SimpleNamespace(id="t", user_phone=USER, callee_name=STORE.name, status=None)
    transcript = [("agent", "Hi, this is Siyona."), ("callee", "Yes we have it.")]
    asyncio.run(
        StatusNotifier(outbox, 60).call_finished(
            task, CallOutcome.COMPLETED, "In stock.", transcript
        )
    )
    assert outbox.texts_to(USER) == [
        "✅ Done. In stock.",
        "📝 Transcript of Main Street Pharmacy:\nSiyona: Hi, this is Siyona.\nThem: Yes we have it.",
    ]


# --- the report endpoint ---------------------------------------------------------------


@pytest.fixture()
def report_app():
    outbox, phone = MockWhatsApp(), FakeTelephony()
    app = create_app(whatsapp=outbox, dialer=CallDispatcher(phone, KEY), call_retry_delay_s=0)
    task = _places_task()
    task.call_id, task.place_id = "call-1", STORE.place_id
    return app, outbox, task


def _report(client: TestClient, token: str | None, call_id: str = "call-1", reason="task_complete"):
    headers = {"X-Call-Token": token} if token else {}
    body = {
        "call_id": call_id,
        "reason": reason,
        "outcome_summary": "They have it in stock.",
        "transcript": [["agent", "Hi, this is Siyona."], ["callee", "We have it."]],
    }
    return client.post("/calls/report", json=body, headers=headers)


def _token(call_id: str = "call-1", purpose: str = call_context.REPORT_TOKEN_PURPOSE) -> str:
    return signing.sign(KEY, purpose, call_id, 60)


@pytest.mark.parametrize(
    "token",
    [None, "garbage", signing.sign("other-key", "call-report", "call-1", 60)],
    ids=["missing", "garbage", "wrong-key"],
)
def test_report_without_a_valid_token_is_rejected(report_app, token) -> None:
    app, outbox, task = report_app
    with TestClient(app) as client:
        assert _report(client, token).status_code == 403
    assert outbox.outbox == [] and task.status is TaskStatus.IN_PROGRESS


def test_report_token_for_another_purpose_or_call_is_rejected(report_app) -> None:
    app, outbox, _ = report_app
    with TestClient(app) as client:
        assert _report(client, _token(purpose="media-stream")).status_code == 403
        assert _report(client, _token("call-2")).status_code == 403
    assert outbox.outbox == []


def test_report_sends_summary_and_transcript_and_remembers_the_call(report_app) -> None:
    app, outbox, task = report_app
    with TestClient(app) as client:
        assert _report(client, _token()).json() == {"status": "ok"}
        assert _report(client, _token()).json() == {"status": "already_reported"}

    assert task.status is TaskStatus.COMPLETED
    assert outbox.texts_to(USER) == [
        "✅ Done. They have it in stock.",
        "📝 Transcript of Main Street Pharmacy:\nSiyona: Hi, this is Siyona.\nThem: We have it.",
    ]
    user = session.session_for(USER)
    assert (user.last_call_task_id, user.last_call_place_id) == (task.id, STORE.place_id)
    assert "They have it in stock." in user.history[-1]["content"]


def test_voicemail_report_is_retried_like_no_answer(report_app) -> None:
    app, outbox, task = report_app
    with TestClient(app) as client:
        _report(client, _token(), reason="voicemail")
    assert outbox.texts_to(USER)[0] == messages.retrying("no_answer", STORE.name, 1, 3, 1)


def test_report_for_an_unknown_call_is_ignored(report_app) -> None:
    app, _, _ = report_app
    with TestClient(app) as client:
        assert _report(client, _token("nope"), call_id="nope").json() == {"status": "unknown_call"}


# --- user name from the WhatsApp envelope ---------------------------------------------


def test_profile_name_is_read_from_the_envelope() -> None:
    import json

    [message] = parse_messages(json.loads(whatsapp_envelope(USER, "hi", "  Asha   K ")))
    assert message.profile_name == "Asha K"


def test_profile_name_reaches_the_assistant_session() -> None:
    outbox = MockWhatsApp()
    model = ScriptedModel([say("Hi Asha!")])
    app = create_app(
        whatsapp=outbox,
        dialer=CallDispatcher(FakeTelephony(), KEY),
        assistant=Assistant(model, MockMaps()),
    )
    body = whatsapp_envelope(USER, "hello", "Asha K")
    with TestClient(app) as client:
        client.post(
            "/webhook/whatsapp",
            content=body,
            headers={SIGNATURE_HEADER: compute_signature(body), "Content-Type": "application/json"},
        )
    assert session.session_for(USER).user_name == "Asha K"
    assert "Customer name (from WhatsApp): Asha K" in model.requests[0][0]["content"]
