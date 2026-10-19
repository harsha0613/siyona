"""What the voice agent knows about the call it is on, and what it reports back.

The dispatcher registers a :class:`CallContext` under the call id before it
dials; when the callee answers and the media stream connects with that call id,
the voice service builds its pipeline from the context. Nothing about the user
travels through the telephony vendor.

- The call opens with an AI disclosure naming the user it calls for.
- The system prompt carries the task, the details the user gave, and the
  user's name and location, and tells the model never to invent anything else.
- :func:`run_call` runs the streaming pipeline with the call tools and returns a
  :class:`CallReport` (end_call reason, summary, transcript) that is posted to
  the gateway's ``/calls/report`` with the context's signed report token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pipeline import CallPipeline
from tools import tool_schemas
from vendors.base import AudioOut, LanguageModel, SpeechToText, TextToSpeech

REPORT_TOKEN_PURPOSE = "call-report"
REPORT_PATH = "/calls/report"
# The callee hung up (or the line dropped) before the model called end_call.
HUNG_UP_REASON = "hung_up"


@dataclass(frozen=True)
class CallContext:
    call_id: str
    task_id: str
    goal: str
    business_name: str | None = None
    details: str | None = None
    user_name: str | None = None
    user_location: str | None = None
    attempt: int = 1
    report_token: str = field(default="", repr=False)


def opening_line(context: CallContext) -> str:
    """The first thing said on every call: who we are, and that we are an AI."""
    on_behalf = f"on behalf of {context.user_name}" if context.user_name else "for a customer"
    return f"Hi, this is Siyona, an AI assistant calling {on_behalf}."


def call_prompt(context: CallContext) -> str:
    lines = [
        "You are Siyona, an AI assistant on a live phone call on behalf of a customer. "
        "You have already said that you are an AI assistant. Speak naturally and keep "
        "every reply to one or two short sentences.",
        "",
        f"TASK: {context.goal}",
    ]
    if context.business_name:
        lines.append(f"CALLING: {context.business_name}")
    if context.user_name:
        lines.append(f"CUSTOMER NAME: {context.user_name}")
    if context.user_location:
        lines.append(f"CUSTOMER LOCATION: {context.user_location}")
    if context.details:
        lines.append(f"DETAILS FROM THE CUSTOMER: {context.details}")
    lines += [
        "",
        "RULES",
        "- Only use the details above. If asked for something you don't have, say you'll "
        "check with the customer; never invent it.",
        "- Never agree to pay, sign up or commit to anything the task doesn't ask for.",
        "- When the task is done, impossible, or they want to hang up, call end_call with a "
        "one or two sentence outcome_summary for the customer.",
    ]
    return "\n".join(lines)


CALL_CONTEXTS: dict[str, CallContext] = {}


def register(context: CallContext) -> None:
    CALL_CONTEXTS[context.call_id] = context


def context_for(call_id: str) -> CallContext | None:
    return CALL_CONTEXTS.get(call_id)


@dataclass(frozen=True)
class CallReport:
    call_id: str
    reason: str
    outcome_summary: str | None
    transcript: tuple[tuple[str, str], ...] = ()

    def as_json(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "reason": self.reason,
            "outcome_summary": self.outcome_summary,
            "transcript": [list(line) for line in self.transcript],
        }


async def run_call(
    context: CallContext,
    *,
    stt: SpeechToText,
    llm: LanguageModel,
    tts: TextToSpeech,
    audio_out: AudioOut,
    **pipeline_options: Any,
) -> CallReport:
    """Hold the conversation for ``context`` and report how it ended."""
    pipeline = CallPipeline(
        stt=stt,
        llm=llm,
        tts=tts,
        audio_out=audio_out,
        system_prompt=call_prompt(context),
        opening_line=opening_line(context),
        tools=tool_schemas(),
        **pipeline_options,
    )
    await pipeline.run()
    ended = pipeline.ended or {}
    return CallReport(
        call_id=context.call_id,
        reason=str(ended.get("reason") or HUNG_UP_REASON),
        outcome_summary=ended.get("outcome_summary"),
        transcript=tuple(pipeline.transcript()),
    )
