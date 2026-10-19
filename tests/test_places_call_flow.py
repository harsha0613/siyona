"""[A3] End to end: search -> details -> call -> WhatsApp summary -> follow-up call.

Everything goes through the real boundaries: signed WhatsApp webhooks in, the
assistant (driven by a scripted model) with MockMaps, the confirmation gate, the
dispatcher and a fake telephony provider, signed telephony status callbacks, the
streaming call pipeline, and the signed /calls/report endpoint back out.
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlencode

import call_context
import pytest
import session
import tasks
from assistant import Assistant
from call_context import run_call
from dispatcher import CallDispatcher
from fakes import FakeTelephony, ScriptedModel, call, say, tool_results, whatsapp_envelope
from fastapi.testclient import TestClient
from maps import MockMaps, Place, PlaceDetails
from models import TaskStatus
from vendors.base import TextDelta
from vendors.mock import MockTextToSpeech, RecordingAudioOut, ScriptedSpeechToText
from webhook import SIGNATURE_HEADER, compute_signature, create_app
from whatsapp import MockWhatsApp

USER = "14155550100"
ADDRESS = "950 Main St, Worcester, MA"
STORE_E164 = "+15085550142"

PLACES = [
    Place("ChIJpharmacyAAAA0001", "Main Street Pharmacy", "12 Main St, Worcester, MA", 4.6),
    Place("ChIJpharmacyBBBB0002", "Park Avenue Drug", "401 Park Ave, Worcester, MA", 4.1),
]
DETAILS = PlaceDetails(
    place_id="ChIJpharmacyAAAA0001",
    name="Main Street Pharmacy",
    address="12 Main St, Worcester, MA",
    phone="(508) 555-0142",
    international_phone="+1 508-555-0142",
    opening_hours=("Monday: 8:00 AM – 9:00 PM",),
)


@pytest.fixture(autouse=True)
def _clean_state():
    for store in (tasks.TASK_STORE, session.SESSIONS, call_context.CALL_CONTEXTS):
        store.clear()
    yield
    for store in (tasks.TASK_STORE, session.SESSIONS, call_context.CALL_CONTEXTS):
        store.clear()


def _list_results(conversation: list[dict]) -> list:
    results = tool_results(conversation)[-1]["results"]
    return say("\n".join(f"{r['result_number']}. {r['name']}" for r in results))


def _ask_pre_call_questions(conversation: list[dict]) -> list:
    details = tool_results(conversation)[-1]
    return say(
        f"{details['name']} is open ({details['opening_hours'][0]}). Before I call: is the "
        "prescription under Asha K, and what strength do you need?"
    )


ASSISTANT_SCRIPT = [
    # "I'm at 950 Main St..., find a pharmacy near me"
    [call("set_user_location", address=ADDRESS)],
    [call("google_places", "c2", query="pharmacy")],
    _list_results,
    # "Call the first one and ask if they have amoxicillin"
    [call("place_details", place="the first one")],
    _ask_pre_call_questions,
    # "Yes, Asha K, 500 mg, pickup"
    [
        call(
            "create_call_request",
            task="Ask if they have amoxicillin 500 mg in stock for pickup",
            details="Customer name: Asha K. Medication: amoxicillin 500 mg. Pickup.",
            questions_asked=True,
        )
    ],
    say("(not sent: the confirmation summary is the reply)"),
    # "Great, yes, book it for 5pm"
    [
        call(
            "create_call_request",
            task="Reserve amoxicillin 500 mg for pickup at 5pm",
            details="Customer name: Asha K. Medication: amoxicillin 500 mg. Pickup at 5pm.",
            questions_asked=True,
        )
    ],
    say("(not sent)"),
]


def _call_script() -> ScriptedModel:
    return ScriptedModel(
        [
            say("Do you have amoxicillin 500 milligrams in stock?"),
            [
                TextDelta("Thank you, goodbye."),
                call(
                    "end_call",
                    reason="task_complete",
                    outcome_summary="Main Street Pharmacy has amoxicillin 500 mg in stock.",
                ),
            ],
        ]
    )


class Harness:
    def __init__(self, client: TestClient, outbox: MockWhatsApp, phone: FakeTelephony) -> None:
        self.client, self.outbox, self.phone = client, outbox, phone

    def whatsapp(self, text: str) -> dict:
        body = whatsapp_envelope(USER, text, "Asha K")
        headers = {SIGNATURE_HEADER: compute_signature(body), "Content-Type": "application/json"}
        response = self.client.post("/webhook/whatsapp", content=body, headers=headers)
        assert response.status_code == 200
        return response.json()

    def status(self, call_id: str, status: str) -> None:
        params = {"call_id": call_id, "CallStatus": status}
        response = self.client.post(
            f"/telephony/status?{urlencode({'call_id': call_id})}",
            content=urlencode(params),
            headers={
                "X-Fake-Signature": "ok",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert response.json() == {"status": "ok"}

    def wait_for_calls(self, count: int) -> None:
        deadline = time.monotonic() + 2.0
        while len(self.phone.placed) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(self.phone.placed) == count

    def answer(self, call_id: str, callee_lines: list[str]) -> dict:
        """The callee picks up: run the real pipeline on the registered context, then report."""
        context = call_context.context_for(call_id)
        assert context is not None
        report = asyncio.run(
            run_call(
                context,
                stt=ScriptedSpeechToText(callee_lines),
                llm=_call_script(),
                tts=MockTextToSpeech(),
                audio_out=RecordingAudioOut(),
                speculate=False,
            )
        )
        response = self.client.post(
            call_context.REPORT_PATH,
            json=report.as_json(),
            headers={"X-Call-Token": context.report_token},
        )
        assert response.json() == {"status": "ok"}
        return report.as_json()


def test_search_details_call_and_whatsapp_summary_then_follow_up() -> None:
    outbox, phone = MockWhatsApp(), FakeTelephony()
    maps = MockMaps(list(PLACES), {DETAILS.place_id: DETAILS})
    assistant = Assistant(ScriptedModel(ASSISTANT_SCRIPT), maps)
    app = create_app(
        whatsapp=outbox,
        dialer=CallDispatcher(phone, "integration-key"),
        assistant=assistant,
        call_retry_delay_s=0,
    )

    with TestClient(app) as client:
        h = Harness(client, outbox, phone)

        # 1. Search near the saved address.
        assert h.whatsapp(f"I'm at {ADDRESS}, find a pharmacy near me")["status"] == (
            "assistant_replied"
        )
        assert maps.queries == [f"pharmacy near {ADDRESS}"]
        assert outbox.texts_to(USER)[-1] == "1. Main Street Pharmacy\n2. Park Avenue Drug"

        # 2. Details, then the pre-call questions; nothing is dialled yet.
        h.whatsapp("Call the first one and ask if they have amoxicillin")
        assert "Before I call: is the prescription under Asha K" in outbox.texts_to(USER)[-1]
        assert phone.placed == []

        # 3. Answers -> the confirmation gate's summary (and only that) goes out.
        sent_before = len(outbox.texts_to(USER))
        h.whatsapp("Yes, Asha K, 500 mg, pickup")
        new = outbox.texts_to(USER)[sent_before:]
        assert len(new) == 1 and new[0].startswith("Before I call")
        assert "Main Street Pharmacy on +1 508-555-0142" in new[0]
        assert phone.placed == []

        # 4. YES dials the number Google listed, with the user's name and location.
        h.whatsapp("yes")
        [first] = tasks.TASK_STORE.values()
        assert phone.placed == [(STORE_E164, first.call_id)]
        context = call_context.context_for(first.call_id)
        assert (context.user_name, context.user_location) == ("Asha K", ADDRESS)

        # 5. Busy: Siyona tells the user and redials the same number for the same task.
        busy_call = first.call_id
        h.status(busy_call, "busy")
        h.wait_for_calls(2)
        assert phone.placed[1] == (STORE_E164, first.call_id)
        assert first.call_id != busy_call and first.attempt == 2
        assert outbox.texts_to(USER)[-1].startswith("🔁 The line was busy")

        # 6. The store answers: disclosure first, then the task; outcome + transcript on WhatsApp.
        report = h.answer(
            first.call_id, ["Main Street Pharmacy, how can I help", "Yes, we have it"]
        )
        assert report["transcript"][0] == [
            "agent",
            "Hi, this is Siyona, an AI assistant calling on behalf of Asha K.",
        ]
        summary, transcript = outbox.texts_to(USER)[-2:]
        assert summary == "✅ Done. Main Street Pharmacy has amoxicillin 500 mg in stock."
        assert transcript.startswith("📝 Transcript of Main Street Pharmacy:\nSiyona: Hi, this is")
        assert "Them: Yes, we have it" in transcript
        assert first.status is TaskStatus.COMPLETED

        # 7. "yes, book it" -> a confirmed follow-up call to the same store with the new detail.
        h.whatsapp("Great, yes, book it for 5pm")
        follow_up = next(t for t in tasks.TASK_STORE.values() if t.id != first.id)
        assert follow_up.status is TaskStatus.AWAITING_CONFIRMATION
        assert follow_up.follow_up_of == first.id
        assert "Pickup at 5pm" in outbox.texts_to(USER)[-1]
        assert len(phone.placed) == 2

        h.whatsapp("yes")
        assert phone.placed[-1] == (STORE_E164, follow_up.call_id)
        follow_up_context = call_context.context_for(follow_up.call_id)
        assert follow_up_context.goal == "Reserve amoxicillin 500 mg for pickup at 5pm"
        assert "Pickup at 5pm" in follow_up_context.details

    # No place id ever reached the user.
    assert not any("ChIJ" in text for text in outbox.texts_to(USER))
