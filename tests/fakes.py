"""Shared test doubles for the WhatsApp assistant."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

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
