"""Signature validation and task creation for the WhatsApp webhook."""

from __future__ import annotations

import json

import pytest
import webhook
from fastapi.testclient import TestClient
from webhook import SIGNATURE_HEADER, compute_signature, create_app, parse_message

SECRET = "dev-secret-not-for-production"


def _envelope(text: str = "Book me a table for four at 7pm", from_phone: str = "15551230000"):
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
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    webhook.TASK_STORE.clear()
    return TestClient(create_app())


def _post(client: TestClient, payload: dict, signature: str | None):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    return client.post("/webhook/whatsapp", content=body, headers=headers)


def test_valid_signature_creates_task(client: TestClient) -> None:
    payload = _envelope()
    body = json.dumps(payload).encode("utf-8")
    response = _post(client, payload, compute_signature(body, SECRET))

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert data["task"]["user_phone"] == "15551230000"
    assert data["task"]["instruction"] == "Book me a table for four at 7pm"
    assert data["task"]["status"] == "received"
    assert len(webhook.TASK_STORE) == 1


def test_bad_signature_returns_401(client: TestClient) -> None:
    response = _post(client, _envelope(), "sha256=" + "0" * 64)

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid signature"
    assert webhook.TASK_STORE == {}


def test_missing_signature_header_returns_401(client: TestClient) -> None:
    response = _post(client, _envelope(), None)

    assert response.status_code == 401
    assert webhook.TASK_STORE == {}


def test_signature_from_a_different_secret_returns_401(client: TestClient) -> None:
    payload = _envelope()
    body = json.dumps(payload).encode("utf-8")
    response = _post(client, payload, compute_signature(body, "some-other-secret"))

    assert response.status_code == 401


def test_tampered_body_returns_401(client: TestClient) -> None:
    signed = json.dumps(_envelope("original")).encode("utf-8")
    signature = compute_signature(signed, SECRET)
    response = _post(client, _envelope("tampered"), signature)

    assert response.status_code == 401


def test_status_callback_is_ignored_not_rejected(client: TestClient) -> None:
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
    body = json.dumps(payload).encode("utf-8")
    response = _post(client, payload, compute_signature(body, SECRET))

    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert webhook.TASK_STORE == {}


def test_parse_message_rejects_blank_text() -> None:
    with pytest.raises(webhook.MessageParseError):
        parse_message(_envelope("   "))


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
