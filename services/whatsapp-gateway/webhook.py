"""WhatsApp Business API inbound webhook.

Validates the provider's HMAC-SHA256 signature, parses the inbound message
envelope, and creates a Task stub that the orchestrator will pick up. The task
store is in-memory here; the real service writes through to the API service.

- Valid signature + text message: a Task is created with the raw text and the
  sender.
- Invalid or missing signature: 403, and nothing is written.
- A message we can't act on (non-text, empty, missing fields): the error is
  logged and the sender is asked to clarify. Delivery/read receipts carry no
  message and are acknowledged without a reply.
- ``POST /calls/report``: the voice agent's end-of-call report (outcome summary
  and transcript), authenticated by the signed report token minted for that
  call. The user gets the summary and the transcript on WhatsApp.

Run locally:

    uvicorn webhook:app --app-dir services/whatsapp-gateway --reload
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

import call_context
import messages
import signing
from assistant import Assistant, build_assistant
from confirmation import ConfirmationGate, Dialer
from dispatcher import CallDispatcher, signing_key_from_env
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from notifier import StatusNotifier
from phone import PhoneNumberError, find_numbers, region_of, whatsapp_to_e164
from pydantic import BaseModel, Field
from session import session_for
from tasks import TASK_STORE, Task, create_task, find_by_call_id
from telephony import (
    STATUS_CALLBACK_PATH,
    CallOutcome,
    Telephony,
    TelephonyError,
    build_telephony,
)
from whatsapp import WhatsAppSender, build_whatsapp

__all__ = ["TASK_STORE", "Task", "create_task", "create_app", "parse_message", "parse_messages"]

logger = logging.getLogger("siyona.gateway.webhook")

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


class MessageParseError(ValueError):
    """The payload did not contain a text message we can act on."""


@dataclass
class InboundMessage:
    """One user message from the envelope. ``error`` is set when we can't act on it."""

    sender: str | None
    text: str | None
    message_id: str | None = None
    error: str | None = None
    # The sender's WhatsApp profile name, from the envelope's ``contacts``.
    profile_name: str | None = None


def _profile_names(value: dict[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for contact in value.get("contacts") or []:
        if not isinstance(contact, dict):
            continue
        profile = contact.get("profile")
        name = profile.get("name") if isinstance(profile, dict) else None
        if contact.get("wa_id") and isinstance(name, str) and name.strip():
            names[str(contact["wa_id"])] = " ".join(name.split())[:80]
    return names


def _inbound(message: Any) -> InboundMessage:
    if not isinstance(message, dict):
        return InboundMessage(None, None, error="message is not an object")
    sender = message.get("from")
    sender = str(sender) if sender else None
    message_id = message.get("id")
    if sender is None:
        return InboundMessage(None, None, message_id, "message has no sender")
    kind = message.get("type", "text")
    if kind != "text":
        return InboundMessage(sender, None, message_id, f"unsupported message type {kind!r}")
    text_obj = message.get("text")
    body = text_obj.get("body") if isinstance(text_obj, dict) else None
    if not isinstance(body, str):
        return InboundMessage(sender, None, message_id, "text message has no body")
    if not body.strip():
        return InboundMessage(sender, None, message_id, "empty message body")
    return InboundMessage(sender, body, message_id)


def parse_messages(payload: Any) -> list[InboundMessage]:
    """Every user message in a (possibly batched) Cloud API envelope.

    Raises :class:`MessageParseError` if the envelope itself is malformed.
    Returns an empty list for notifications that carry no message (receipts).
    """
    if not isinstance(payload, dict):
        raise MessageParseError("payload is not an object")
    found: list[InboundMessage] = []
    try:
        for entry in payload.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                names = _profile_names(value)
                for message in value.get("messages") or []:
                    inbound = _inbound(message)
                    if inbound.sender is not None:
                        inbound.profile_name = names.get(inbound.sender)
                    found.append(inbound)
    except (AttributeError, TypeError) as exc:
        raise MessageParseError("malformed envelope") from exc
    return found


def parse_message(payload: dict[str, Any]) -> tuple[str, str]:
    """``(sender, text)`` of the first actionable text message. Raises if there is none."""
    for message in parse_messages(payload):
        if message.error is None and message.sender and message.text:
            return message.sender, message.text.strip()
    raise MessageParseError("no text message in payload")


def _for_assistant(assistant: Assistant | None, sender: str, text: str) -> bool:
    """Instructions that carry their own number keep the rule-based intake."""
    if assistant is None:
        return False
    try:
        region = region_of(whatsapp_to_e164(sender))
    except PhoneNumberError:
        region = None
    return not find_numbers(text, region)


async def _assist(assistant: Assistant, sender: WhatsAppSender, message: InboundMessage) -> None:
    to, text = message.sender or "", message.text or ""
    try:
        reply = await assistant.reply(to, text, message.profile_name)
    except Exception:
        logger.exception("assistant failed")
        reply = messages.ASSISTANT_UNAVAILABLE
    if not reply:
        return  # a tool already messaged the user (the call confirmation summary)
    try:
        await sender.send_text(to, reply)
    except Exception:
        logger.exception("could not send assistant reply")


async def _ask_to_clarify(sender: WhatsAppSender, to: str) -> bool:
    try:
        await sender.send_text(to, messages.CLARIFY)
    except Exception:
        logger.exception("could not send clarification prompt")
        return False
    return True


@router.post("/webhook/whatsapp")
async def receive_message(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None, alias=SIGNATURE_HEADER),
) -> dict[str, Any]:
    body = await request.body()
    if not verify_signature(body, x_hub_signature_256):
        logger.warning("rejected webhook with invalid signature")
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = await request.json()
        inbound = parse_messages(payload)
    except (ValueError, MessageParseError) as exc:
        # No trustworthy sender to reply to; log it and tell Meta not to retry.
        logger.error("malformed webhook payload: %s", exc)
        raise HTTPException(status_code=400, detail="malformed payload") from exc

    if not inbound:
        # Delivery/read receipts and other notifications: acknowledge, don't retry.
        return {"status": "ignored", "tasks": [], "clarified": 0}

    sender: WhatsAppSender = request.app.state.whatsapp
    gate: ConfirmationGate = request.app.state.gate
    assistant: Assistant | None = request.app.state.assistant
    tasks: list[dict[str, Any]] = []
    replies = 0
    assisted = 0
    clarified = 0
    for message in inbound:
        if message.error is None and message.sender and message.text:
            if await gate.handle_reply(message.sender, message.text):
                replies += 1
                continue
            if _for_assistant(assistant, message.sender, message.text):
                await _assist(assistant, sender, message)
                assisted += 1
                continue
            task = create_task(message.sender, message.text)
            await gate.open(task)
            tasks.append(task.as_dict())
            continue
        logger.error("could not act on inbound message %s: %s", message.message_id, message.error)
        if message.sender and await _ask_to_clarify(sender, message.sender):
            clarified += 1

    if tasks:
        status = "accepted"
    elif replies:
        status = "reply_handled"
    elif assisted:
        status = "assistant_replied"
    else:
        status = "clarification_requested"
    return {"status": status, "tasks": tasks, "clarified": clarified}


def _public_url(request: Request) -> str:
    """The URL the vendor signed: our public base, not what the load balancer forwarded."""
    base = os.environ.get("GATEWAY_PUBLIC_URL", "").rstrip("/")
    if not base:
        return str(request.url)
    query = f"?{request.url.query}" if request.url.query else ""
    return f"{base}{request.url.path}{query}"


@router.post(STATUS_CALLBACK_PATH)
async def telephony_status(request: Request) -> dict[str, Any]:
    """Call progress from the telephony vendor (verified by the adapter)."""
    telephony: Telephony = request.app.state.telephony
    params = dict(parse_qsl((await request.body()).decode("utf-8", "replace")))
    try:
        progress = telephony.parse_status_callback(
            _public_url(request), params, dict(request.headers)
        )
    except TelephonyError as exc:
        logger.warning("rejected telephony status callback: %s", exc)
        raise HTTPException(status_code=403, detail="invalid callback") from exc

    task = find_by_call_id(progress.call_id)
    if task is None:
        return {"status": "unknown_call"}
    if progress.outcome is not None:
        notifier: StatusNotifier = request.app.state.notifier
        await notifier.call_finished(task, progress.outcome)
    return {"status": "ok"}


class CallReportBody(BaseModel):
    call_id: str = Field(max_length=64)
    reason: str = Field(max_length=32)
    outcome_summary: str | None = Field(default=None, max_length=1000)
    transcript: list[tuple[str, str]] = Field(default_factory=list, max_length=400)


# end_call reasons from the voice agent -> how the call ended, for the notifier.
_REPORT_OUTCOMES = {
    "task_complete": CallOutcome.COMPLETED,
    "task_failed": CallOutcome.COMPLETED,  # answered; the summary says what didn't work
    "human_requested": CallOutcome.COMPLETED,
    "voicemail": CallOutcome.NO_ANSWER,
}


@router.post(call_context.REPORT_PATH)
async def call_report(
    request: Request,
    body: CallReportBody,
    x_call_token: str | None = Header(default=None, alias="X-Call-Token"),
) -> dict[str, Any]:
    """The voice agent's end-of-call report, signed with the call's report token."""
    try:
        subject = signing.verify(
            request.app.state.signing_key, call_context.REPORT_TOKEN_PURPOSE, x_call_token or ""
        )
    except signing.InvalidToken as exc:
        logger.warning("rejected call report: %s", exc)
        raise HTTPException(status_code=403, detail="invalid report token") from exc
    if subject != body.call_id:
        raise HTTPException(status_code=403, detail="token is for another call")

    task = find_by_call_id(body.call_id)
    if task is None:
        return {"status": "unknown_call"}
    outcome = _REPORT_OUTCOMES.get(body.reason, CallOutcome.HUNG_UP)
    summary = (body.outcome_summary or "").strip() or None
    notifier: StatusNotifier = request.app.state.notifier
    reported = await notifier.call_finished(task, outcome, summary, body.transcript)
    if reported and outcome is CallOutcome.COMPLETED:
        session_for(task.user_phone).note_call(
            task.id, task.place_id, task.callee_name, summary or "no summary"
        )
    return {"status": "ok" if reported else "already_reported"}


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def create_app(
    whatsapp: WhatsAppSender | None = None,
    dialer: Dialer | None = None,
    confirmation_timeout_s: float | None = None,
    expected_call_window_s: float | None = None,
    assistant: Assistant | None = None,
    call_retry_delay_s: float | None = None,
) -> FastAPI:
    sender = whatsapp or build_whatsapp()
    if dialer is None:
        dialer = CallDispatcher(build_telephony(), signing_key_from_env())
    telephony = getattr(dialer, "telephony", None) or build_telephony()
    notifier = StatusNotifier(
        sender, expected_call_window_s, redialer=dialer, retry_delay_s=call_retry_delay_s
    )
    gate = ConfirmationGate(sender, dialer, confirmation_timeout_s, watcher=notifier)
    if assistant is None:
        assistant = build_assistant()
    if assistant is not None:
        assistant.gate = gate

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        sweeper = asyncio.create_task(gate.run_expiry_loop(stop))
        try:
            yield
        finally:
            stop.set()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper
            await notifier.aclose()

    application = FastAPI(title="Siyona WhatsApp Gateway", version="0.1.0", lifespan=lifespan)
    application.state.whatsapp = sender
    application.state.gate = gate
    application.state.notifier = notifier
    application.state.telephony = telephony
    application.state.assistant = assistant
    application.state.signing_key = getattr(dialer, "signing_key", None) or signing_key_from_env()
    application.include_router(router)
    return application


app = create_app()
