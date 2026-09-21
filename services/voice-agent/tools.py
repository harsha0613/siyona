"""Tool layer for the voice agent.

The full agent exposes 17 tools to the model. Two reference implementations
live here to pin down the contract every other tool follows:

* a JSON Schema for the arguments, passed straight to the model as a
  ``function`` tool definition;
* a pure-Python handler that validates its arguments and returns a
  JSON-serialisable result dict;
* registration through ``TOOL_REGISTRY`` so the pipeline can dispatch by name.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Digits, star, hash and the pause character understood by the telephony vendor.
DTMF_PATTERN = re.compile(r"^[0-9*#w]{1,32}$")

END_CALL_REASONS = ("task_complete", "task_failed", "human_requested", "voicemail")


@dataclass(frozen=True)
class Tool:
    """A model-callable tool: schema plus the handler that executes it."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., dict[str, Any]]

    def as_openai_schema(self) -> dict[str, Any]:
        """Render the definition in the shape the model API expects."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolError(ValueError):
    """Raised when a tool is called with arguments it cannot honour."""


# ---------------------------------------------------------------------------
# send_dtmf
# ---------------------------------------------------------------------------

SEND_DTMF_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "digits": {
            "type": "string",
            "description": (
                "Digits to dial, e.g. '1' or '4085551234#'. Allowed characters "
                "are 0-9, '*', '#', and 'w' for a half-second pause."
            ),
            "pattern": "^[0-9*#w]{1,32}$",
        },
        "reason": {
            "type": "string",
            "description": "Why this option was chosen, for the transcript.",
            "maxLength": 200,
        },
    },
    "required": ["digits", "reason"],
    "additionalProperties": False,
}


def send_dtmf(digits: str, reason: str) -> dict[str, Any]:
    """Play DTMF tones into the live call to navigate an IVR menu."""
    if not isinstance(digits, str) or not DTMF_PATTERN.match(digits):
        raise ToolError(f"invalid DTMF sequence: {digits!r}")
    if not reason.strip():
        raise ToolError("reason must not be empty")
    return {
        "status": "sent",
        "digits": digits,
        "duration_ms": len(digits) * 120,
    }


# ---------------------------------------------------------------------------
# end_call
# ---------------------------------------------------------------------------

END_CALL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": "Why the call is ending.",
            "enum": list(END_CALL_REASONS),
        },
        "outcome_summary": {
            "type": "string",
            "description": "One or two sentences reported back to the user on WhatsApp.",
            "maxLength": 500,
        },
    },
    "required": ["reason", "outcome_summary"],
    "additionalProperties": False,
}


def end_call(reason: str, outcome_summary: str) -> dict[str, Any]:
    """Hang up and hand the outcome to the orchestrator."""
    if reason not in END_CALL_REASONS:
        raise ToolError(f"unknown end_call reason: {reason!r}")
    if not outcome_summary.strip():
        raise ToolError("outcome_summary must not be empty")
    return {
        "status": "ended",
        "reason": reason,
        "outcome_summary": outcome_summary.strip(),
    }


TOOLS: tuple[Tool, ...] = (
    Tool(
        name="send_dtmf",
        description=(
            "Play DTMF tones into the live call. Use this to select an IVR menu "
            "option or enter a reference number when prompted."
        ),
        parameters=SEND_DTMF_PARAMETERS,
        handler=send_dtmf,
    ),
    Tool(
        name="end_call",
        description=(
            "End the call once the task is done, impossible, or the other party "
            "asks to hang up. Always the final tool call of a call."
        ),
        parameters=END_CALL_PARAMETERS,
        handler=end_call,
    ),
)

TOOL_REGISTRY: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def tool_schemas() -> list[dict[str, Any]]:
    """All tool definitions, ready to pass to the model."""
    return [tool.as_openai_schema() for tool in TOOLS]


def dispatch(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a tool call emitted by the model."""
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        raise ToolError(f"unknown tool: {name!r}")
    return tool.handler(**arguments)
