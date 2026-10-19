"""The WhatsApp assistant: a tool-calling model loop over the user's session.

Each inbound message runs one loop: the model sees the system prompt, the
session's recent history and the message, may call tools from
``assistant_tools`` (bounded by ``MAX_TOOL_ROUNDS``), and its final text is the
WhatsApp reply. Before sending, any place id is scrubbed from the reply as a
second line of defence; the tools never show ids to the model in the first
place.

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
from assistant_tools import ASSISTANT_TOOLS, AssistantTool, ToolContext, dispatch
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
- List at most 5 places, numbered, each with its name, rating and one short detail.
- Never show internal identifiers. Refer to places by number or name.
- Keep replies short; this is WhatsApp.
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
    location = session.location or "not saved yet"
    return f"{ASSISTANT_PROMPT}\nCONTEXT\n- Saved address: {location}\n"


def _tool_message(call: ToolCallRequest, result: dict[str, Any]) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)}


class Assistant:
    def __init__(
        self,
        llm: LanguageModel,
        maps: Maps,
        tools: Sequence[AssistantTool] = ASSISTANT_TOOLS,
        max_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        self._llm = llm
        self.maps = maps
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

    async def reply(self, user_phone: str, text: str) -> str:
        """Run one model loop for ``text`` and return the WhatsApp reply."""
        session = session_for(user_phone)
        ctx = ToolContext(session, self.maps)
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
