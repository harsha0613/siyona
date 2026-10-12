"""Signature validation and task creation for the WhatsApp webhook."""

from __future__ import annotations

import json
import logging

import pytest
import webhook
from dispatcher import CallDispatcher
from fastapi.testclient import TestClient
from messages import CLARIFY
from telephony import MockTelephony
from webhook import SIGNATURE_HEADER, compute_signature, create_app, parse_message
from whatsapp import MockWhatsApp

SECRET = "dev-secret-not-for-production"


def _envelope(text: str = "Book me a table for four at 7pm", from_phone: str = "15550100001"):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "ENTRY_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": "wamid.TEST",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


@pytest.fixture()
def outbox() -> MockWhatsApp:
    return MockWhatsApp()


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, outbox: MockWhatsApp) -> TestClient:
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    webhook.TASK_STORE.clear()
    return TestClient(create_app(whatsapp=outbox, dialer=CallDispatcher(MockTelephony(), "k")))


def _signed_post(client: TestClient, payload):
    body = json.dumps(payload).encode("utf-8")
    return _post(client, payload, compute_signature(body, SECRET))


def _message_envelope(message: dict) -> dict:
    envelope = _envelope()
    envelope["entry"][0]["changes"][0]["value"]["messages"] = [message]
    return envelope


def _post(client: TestClient, payload: dict, signature: str | None):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    return client.post("/webhook/whatsapp", content=body, headers=headers)


def test_valid_signature_creates_task_with_raw_text_and_sender(
    client: TestClient, outbox: MockWhatsApp
) -> None:
    raw = "  Book me a table for four at 7pm  "
    response = _signed_post(client, _envelope(raw))

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    [task] = data["tasks"]
    assert task["user_phone"] == "15550100001"
    assert task["instruction"] == raw
    [stored] = webhook.TASK_STORE.values()
    assert (stored.user_phone, stored.instruction) == ("15550100001", raw)
    assert CLARIFY not in outbox.texts_to("15550100001")


def test_bad_signature_returns_403(client: TestClient) -> None:
    response = _post(client, _envelope(), "sha256=" + "0" * 64)

    assert response.status_code == 403
    assert response.json()["detail"] == "invalid signature"
    assert webhook.TASK_STORE == {}


def test_missing_signature_header_returns_403(client: TestClient) -> None:
    response = _post(client, _envelope(), None)

    assert response.status_code == 403
    assert webhook.TASK_STORE == {}


def test_signature_from_a_different_secret_returns_403(client: TestClient) -> None:
    payload = _envelope()
    body = json.dumps(payload).encode("utf-8")
    response = _post(client, payload, compute_signature(body, "some-other-secret"))

    assert response.status_code == 403
    assert webhook.TASK_STORE == {}


def test_tampered_body_returns_403(client: TestClient) -> None:
    signed = json.dumps(_envelope("original")).encode("utf-8")
    signature = compute_signature(signed, SECRET)
    response = _post(client, _envelope("tampered"), signature)

    assert response.status_code == 403
    assert webhook.TASK_STORE == {}


def test_invalid_signature_sends_nothing(client: TestClient, outbox: MockWhatsApp) -> None:
    _post(client, _message_envelope({"from": "15550100001", "type": "image"}), "sha256=bad")

    assert outbox.outbox == []


def test_status_callback_is_ignored_not_rejected(client: TestClient, outbox: MockWhatsApp) -> None:
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
    response = _signed_post(client, payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert webhook.TASK_STORE == {}
    assert outbox.outbox == []


@pytest.mark.parametrize(
    "message",
    [
        {"from": "15550100001", "id": "wamid.1", "type": "image", "image": {"id": "m1"}},
        {"from": "15550100001", "id": "wamid.2", "type": "text", "text": {"body": "   "}},
        {"from": "15550100001", "id": "wamid.3", "type": "text"},
        {"from": "15550100001", "id": "wamid.4", "type": "text", "text": "not-an-object"},
    ],
    ids=["non-text", "blank", "no-text-field", "text-not-object"],
)
def test_malformed_message_logs_and_asks_sender_to_clarify(
    client: TestClient,
    outbox: MockWhatsApp,
    caplog: pytest.LogCaptureFixture,
    message: dict,
) -> None:
    with caplog.at_level(logging.ERROR, logger="siyona.gateway.webhook"):
        response = _signed_post(client, _message_envelope(message))

    assert response.status_code == 200
    assert response.json()["status"] == "clarification_requested"
    assert webhook.TASK_STORE == {}
    assert outbox.texts_to("15550100001") == [CLARIFY]
    assert any("could not act on inbound message" in r.getMessage() for r in caplog.records)


def test_message_without_sender_is_logged_not_answered(
    client: TestClient, outbox: MockWhatsApp, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="siyona.gateway.webhook"):
        response = _signed_post(client, _message_envelope({"type": "text", "text": {"body": "hi"}}))

    assert response.status_code == 200
    assert webhook.TASK_STORE == {}
    assert outbox.outbox == []
    assert caplog.records


@pytest.mark.parametrize("payload", [[1, 2], {"entry": "nope"}, {"entry": [{"changes": [3]}]}])
def test_malformed_envelope_is_logged_and_rejected(
    client: TestClient, caplog: pytest.LogCaptureFixture, payload
) -> None:
    with caplog.at_level(logging.ERROR, logger="siyona.gateway.webhook"):
        response = _signed_post(client, payload)

    assert response.status_code == 400
    assert webhook.TASK_STORE == {}
    assert any("malformed webhook payload" in r.getMessage() for r in caplog.records)


def test_malformed_json_is_rejected(client: TestClient) -> None:
    body = b"{not json"
    response = client.post(
        "/webhook/whatsapp",
        content=body,
        headers={SIGNATURE_HEADER: compute_signature(body, SECRET)},
    )

    assert response.status_code == 400
    assert webhook.TASK_STORE == {}


def test_valid_and_malformed_messages_in_one_batch(
    client: TestClient, outbox: MockWhatsApp
) -> None:
    envelope = _envelope("Call the dentist")
    envelope["entry"][0]["changes"][0]["value"]["messages"].append(
        {"from": "15550100002", "id": "wamid.5", "type": "audio"}
    )
    response = _signed_post(client, envelope)

    assert response.json()["status"] == "accepted"
    assert len(webhook.TASK_STORE) == 1
    assert outbox.texts_to("15550100002") == [CLARIFY]


def test_clarification_send_failure_does_not_fail_the_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Broken:
        async def send_text(self, to: str, body: str) -> None:
            raise RuntimeError("whatsapp down")

    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    webhook.TASK_STORE.clear()
    client = TestClient(create_app(whatsapp=Broken(), dialer=CallDispatcher(MockTelephony(), "k")))
    response = _signed_post(client, _message_envelope({"from": "15550100001", "type": "video"}))

    assert response.status_code == 200
    assert response.json()["clarified"] == 0


def test_parse_message_rejects_blank_text() -> None:
    with pytest.raises(webhook.MessageParseError):
        parse_message(_envelope("   "))


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
