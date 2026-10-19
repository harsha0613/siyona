"""Shared test doubles for the WhatsApp assistant."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from telephony import CallOutcome, CallProgress, TelephonyError
from vendors.base import LlmEvent, TextDelta, ToolCallRequest

Turn = list[LlmEvent] | Callable[[list[dict[str, Any]]], list[LlmEvent]]


def call(name: str, call_id: str = "call_1", **arguments: Any) -> ToolCallRequest:
    return ToolCallRequest(call_id, name, json.dumps(arguments))


def say(text: str) -> list[LlmEvent]:
    return [TextDelta(text)]


def tool_results(conversation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every tool result the model has been shown so far, decoded."""
    return [json.loads(m["content"]) for m in conversation if m["role"] == "tool"]


class ScriptedModel:
    """Plays one scripted turn per model request and records what it was sent.

    A turn is a list of events, or a function of the conversation so a test can
    answer from the tool results the model was actually given.
    """

    def __init__(self, turns: list[Turn]) -> None:
        self._turns = list(turns)
        self.requests: list[list[dict[str, Any]]] = []
        self.tools: list[dict[str, Any]] = []

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[LlmEvent]:
        self.requests.append([dict(m) for m in messages])
        self.tools = tools
        turn = self._turns.pop(0)
        for event in turn(messages) if callable(turn) else turn:
            yield event


class FakeTelephony:
    """A telephony provider for end-to-end tests.

    Implements the Sprint 1 ``Telephony`` protocol: records every call it is
    asked to place, and accepts status callbacks that carry ``X-Fake-Signature:
    ok`` (anything else is rejected, like a forged vendor callback).
    """

    OUTCOMES = {
        "busy": CallOutcome.BUSY,
        "no-answer": CallOutcome.NO_ANSWER,
        "failed": CallOutcome.FAILED,
        "completed": CallOutcome.HUNG_UP,
    }

    def __init__(self) -> None:
        self.placed: list[tuple[str, str]] = []
        self.hung_up: list[str] = []

    async def place_call(self, *, to: str, call_id: str, stream_token: str) -> str:
        self.placed.append((to, call_id))
        return f"FAKE{len(self.placed)}"

    async def hang_up(self, provider_call_sid: str) -> None:
        self.hung_up.append(provider_call_sid)

    def parse_status_callback(
        self, url: str, params: dict[str, str], headers: dict[str, str]
    ) -> CallProgress:
        if {k.lower(): v for k, v in headers.items()}.get("x-fake-signature") != "ok":
            raise TelephonyError("forged")
        status = params.get("CallStatus", "")
        return CallProgress(params["call_id"], self.OUTCOMES.get(status), status == "in-progress")


def whatsapp_envelope(sender: str, text: str, profile_name: str | None = None) -> bytes:
    value: dict[str, Any] = {
        "messages": [{"from": sender, "id": "wamid.T", "type": "text", "text": {"body": text}}]
    }
    if profile_name:
        value["contacts"] = [{"wa_id": sender, "profile": {"name": profile_name}}]
    return json.dumps({"entry": [{"changes": [{"value": value}]}]}).encode()
