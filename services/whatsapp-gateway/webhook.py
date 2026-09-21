"""WhatsApp Business API inbound webhook.

Validates the provider's HMAC-SHA256 signature, parses the inbound message
envelope, and creates a Task stub that the orchestrator will pick up. The task
store is in-memory here; the real service writes through to the API service.

Run locally:

    uvicorn webhook:app --app-dir services/whatsapp-gateway --reload
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request

SIGNATURE_HEADER = "X-Hub-Signature-256"
SIGNATURE_PREFIX = "sha256="

router = APIRouter()


def app_secret() -> str:
    """Shared secret used to sign webhook payloads.

    Falls back to a well-known development value so the service boots without
    configuration; production deployments must set WHATSAPP_APP_SECRET.
    """
    return os.environ.get("WHATSAPP_APP_SECRET", "dev-secret-not-for-production")


def compute_signature(body: bytes, secret: str | None = None) -> str:
    """Return the ``sha256=<hex>`` signature header value for ``body``."""
    key = (secret if secret is not None else app_secret()).encode("utf-8")
    digest = hmac.new(key, body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(body: bytes, header_value: str | None, secret: str | None = None) -> bool:
    """Constant-time check of an inbound signature header."""
    if not header_value or not header_value.startswith(SIGNATURE_PREFIX):
        return False
    return hmac.compare_digest(compute_signature(body, secret), header_value)


@dataclass
class Task:
    """Minimal task stub created from an inbound WhatsApp message."""

    id: str
    user_phone: str
    instruction: str
    status: str = "received"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_phone": self.user_phone,
            "instruction": self.instruction,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
        }


# Session state keyed by task id. Replaced by Redis + the API service in R2.
TASK_STORE: dict[str, Task] = {}


class MessageParseError(ValueError):
    """The payload did not contain a text message we can act on."""


def parse_message(payload: dict[str, Any]) -> tuple[str, str]:
    """Pull ``(from_phone, text)`` out of a WhatsApp Cloud API envelope."""
    try:
        value = payload["entry"][0]["changes"][0]["value"]
        message = value["messages"][0]
        from_phone = message["from"]
        text = message["text"]["body"]
    except (KeyError, IndexError, TypeError) as exc:
        raise MessageParseError("no text message in payload") from exc

    if not isinstance(text, str) or not text.strip():
        raise MessageParseError("empty message body")
    return from_phone, text.strip()


def create_task(user_phone: str, instruction: str) -> Task:
    """Create and store a Task stub for the orchestrator to fan out."""
    task = Task(id=str(uuid.uuid4()), user_phone=user_phone, instruction=instruction)
    TASK_STORE[task.id] = task
    return task


@router.post("/webhook/whatsapp")
async def receive_message(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias=SIGNATURE_HEADER),
) -> dict[str, Any]:
    body = await request.body()
    if not verify_signature(body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="malformed JSON") from exc

    try:
        user_phone, instruction = parse_message(payload)
    except MessageParseError as exc:
        # Status callbacks and non-text messages are acknowledged, not retried.
        return {"status": "ignored", "reason": str(exc)}

    task = create_task(user_phone, instruction)
    return {"status": "accepted", "task": task.as_dict()}


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def create_app() -> FastAPI:
    application = FastAPI(title="Siyona WhatsApp Gateway", version="0.1.0")
    application.include_router(router)
    return application


app = create_app()
