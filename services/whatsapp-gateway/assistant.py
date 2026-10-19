"""The WhatsApp assistant: a tool-calling model loop over the user's session.

Each inbound message runs one loop: the model sees the system prompt, the
session's recent history and the message, may call tools from
``assistant_tools`` (bounded by ``MAX_TOOL_ROUNDS``), and its final text is the
WhatsApp reply. Before sending, any place id is scrubbed from the reply as a
second line of defence; the tools never show ids to the model in the first
place.

A turn that opens a call confirmation sends only the gate's who / what / when
summary; the call itself waits for the user's YES, exactly like a typed number.

The assistant is on when ``OPENAI_API_KEY`` is set. Without a key the gateway
keeps its rule-based intake, so local runs and CI need no keys.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

import messages
from assistant_tools import ASSISTANT_TOOLS, AssistantTool, Confirmer, ToolContext, dispatch
from maps import Maps, build_maps
from session import Session, session_for
from tools import ToolError
from vendors.base import LanguageModel, TextDelta, ToolCallRequest
from vendors.factory import VoiceVendorConfig

logger = logging.getLogger("siyona.gateway.assistant")

MAX_TOOL_ROUNDS = 5

ASSISTANT_PROMPT = """\
You are Siyona, a WhatsApp assistant that finds local businesses and calls them on the \
user's behalf.

RULES
- NEVER make up business names, addresses, phone numbers, ratings or opening hours. Every \
business you mention must come from a google_places result in this conversation. If a \
search finds nothing, say so.
- To find or recommend any store, restaurant or service, call google_places. "Near me" \
uses the saved address; if there is none, ask the user where they are.
- When the user tells you where they are, call set_user_location before searching.
- For a place's phone number, hours, website or reviews, call place_details. "The first \
one" is result 1. If a field is "Not available", say it is not available; never fill it in.
- For "how far", "how long to get there" or directions, call get_directions; "there" is \
the place last discussed and the trip starts from the saved address. Use driving unless \
the user names walking, transit or bicycling. Give distance and ETA first, then steps.
- List at most 5 places, numbered, each with its name, rating and one short detail.
- Never show internal identifiers. Refer to places by number or name.
- Keep replies short; this is WhatsApp.

CALLING A STORE
- Offer to call a store you found. You never choose or type a phone number: \
create_call_request dials the number Google lists for the place from the last \
place_details lookup, so look the store up first.
- Before create_call_request, ask the 2-3 most important questions for this kind of \
business in ONE message (e.g. pharmacy: medication and strength, pickup or delivery; \
restaurant: party size, date and time). Confirm the customer's name if you know it. Skip \
anything already answered; if the user says "just call", call with what you have.
- Put every answer in details. After create_call_request the user gets a confirmation \
summary and must reply YES; do not repeat it.
- After a call result, if the user wants more done (e.g. "yes, book it"), call \
create_call_request again for the same store with the updated task and details.
"""

# Google place ids: "ChIJ…" for most places, plus any "[place_id:…]" style leak.
_PLACE_ID_RE = re.compile(r"\[?\s*place[_ ]id\s*[:=]\s*[\w-]*\s*\]?|\bChIJ[\w-]{8,}", re.IGNORECASE)


def scrub_place_ids(text: str, known_ids: set[str]) -> str:
    """Remove every place id from a reply bound for WhatsApp."""
    text = _PLACE_ID_RE.sub("", text)
    for place_id in known_ids:
        text = text.replace(place_id, "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"[ \t]+([.,;:!?])", r"\1", text).strip()


def _context_prompt(session: Session) -> str:
    lines = [f"- Saved address: {session.location or 'not saved yet'}"]
    if session.user_name:
        lines.append(f"- Customer name (from WhatsApp): {session.user_name}")
    if session.current_place is not None:
        lines.append(f"- Place last looked up: {session.current_place.name}")
    return f"{ASSISTANT_PROMPT}\nCONTEXT\n" + "\n".join(lines) + "\n"


CONFIRMATION_SENT_NOTE = "(Sent the call confirmation summary; waiting for YES.)"


def _tool_message(call: ToolCallRequest, result: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}


class Assistant:
    def __init__(
        self,
        llm: LanguageModel,
        maps: Maps,
        tools: Sequence[AssistantTool] = ASSISTANT_TOOLS,
        max_rounds: int = MAX_TOOL_ROUNDS,
        gate: Confirmer | None = None,
    ) -> None:
        self._llm = llm
        self.maps = maps
        # Set by the gateway once its confirmation gate exists.
        self.gate = gate
        self._schemas = [tool.as_openai_schema() for tool in tools]
        self._max_rounds = max_rounds

    async def _run_tool(self, ctx: ToolContext, call: ToolCallRequest) -> dict[str, Any]:
        try:
            arguments = json.loads(call.arguments or "{}")
            if not isinstance(arguments, dict):
                raise ToolError("arguments must be a JSON object")
            return await dispatch(ctx, call.name, arguments)
        except (ToolError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("tool %s rejected: %s", call.name, exc)
            return {"status": "error", "message": str(exc)}

    async def reply(self, user_phone: str, text: str, user_name: str | None = None) -> str:
        """Run one model loop for ``text`` and return the WhatsApp reply.

        Returns an empty string when a tool already messaged the user this turn (the
        call confirmation summary), so the gateway sends nothing more.
        """
        session = session_for(user_phone)
        if user_name:
            session.user_name = user_name
        ctx = ToolContext(session, self.maps, self.gate)
        conversation: list[dict[str, Any]] = [
            {"role": "system", "content": _context_prompt(session)},
            *session.history,
            {"role": "user", "content": text},
        ]
        for _ in range(self._max_rounds):
            parts: list[str] = []
            calls: list[ToolCallRequest] = []
            async for event in self._llm.stream(conversation, self._schemas):
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                elif isinstance(event, ToolCallRequest):
                    calls.append(event)
            if not calls:
                if ctx.confirmation_opened:
                    session.remember(text, CONFIRMATION_SENT_NOTE)
                    return ""
                reply = scrub_place_ids("".join(parts), session.place_ids)
                reply = reply or messages.ASSISTANT_UNAVAILABLE
                session.remember(text, reply)
                return reply
            conversation.append(
                {
                    "role": "assistant",
                    "content": "".join(parts) or None,
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": call.arguments},
                        }
                        for call in calls
                    ],
                }
            )
            for call in calls:
                conversation.append(_tool_message(call, await self._run_tool(ctx, call)))
        logger.warning("assistant gave up after %d tool rounds", self._max_rounds)
        return messages.ASSISTANT_UNAVAILABLE


def build_assistant(config: VoiceVendorConfig | None = None) -> Assistant | None:
    """The live assistant when an OpenAI key is configured, otherwise None."""
    config = config or VoiceVendorConfig.from_env()
    if not config.openai_api_key:
        logger.info("OPENAI_API_KEY not set; WhatsApp assistant disabled")
        return None
    from openai import AsyncOpenAI
    from vendors.openai_llm import OpenAIChatModel

    client = AsyncOpenAI(api_key=config.openai_api_key, timeout=20.0, max_retries=1)
    return Assistant(OpenAIChatModel(client, config.llm_model, max_tokens=500), build_maps())
