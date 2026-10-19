"""Tools the WhatsApp assistant's model may call.

Same contract as the call-time tools in ``tools.py`` (JSON Schema arguments, a
handler returning a JSON-serialisable dict), with two differences: handlers are
async, and each receives a :class:`ToolContext` holding the user's session and
the Maps client.

Place ids stay out of the model's view entirely. ``google_places`` returns
numbered results and keeps the ids in the session, so the model can only refer
to a place it was actually shown, and has nothing to leak into a reply.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from maps import Maps, MapsError, Place
from session import Session
from tools import Tool, ToolError

MAX_LOCATION_CHARS = 200
MAX_QUERY_CHARS = 120


@dataclass(frozen=True)
class ToolContext:
    session: Session
    maps: Maps


@dataclass(frozen=True)
class AssistantTool(Tool):
    handler: Callable[..., Awaitable[dict[str, Any]]]


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{name} must not be empty")
    value = " ".join(value.split())
    if len(value) > limit:
        raise ToolError(f"{name} is longer than {limit} characters")
    return value


# ---------------------------------------------------------------------------
# set_user_location
# ---------------------------------------------------------------------------

SET_USER_LOCATION_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "address": {
            "type": "string",
            "description": (
                "The address or area exactly as the user gave it, e.g. "
                "'950 Main St, Worcester, MA' or 'Back Bay, Boston'."
            ),
            "maxLength": MAX_LOCATION_CHARS,
        },
    },
    "required": ["address"],
    "additionalProperties": False,
}


async def set_user_location(ctx: ToolContext, address: str) -> dict[str, Any]:
    """Save the user's address as the default for "near me" searches."""
    ctx.session.location = _text(address, "address", MAX_LOCATION_CHARS)
    return {"status": "saved", "location": ctx.session.location}


# ---------------------------------------------------------------------------
# google_places
# ---------------------------------------------------------------------------

GOOGLE_PLACES_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "What to look for, e.g. 'pharmacy' or 'vegan bakery'.",
            "maxLength": MAX_QUERY_CHARS,
        },
        "near": {
            "type": "string",
            "description": (
                "Where to search. Leave out for 'near me' so the user's saved address is used."
            ),
            "maxLength": MAX_LOCATION_CHARS,
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def _result(number: int, place: Place) -> dict[str, Any]:
    result: dict[str, Any] = {"result_number": number, "name": place.name}
    if place.address:
        result["address"] = place.address
    if place.rating is not None:
        result["rating"] = place.rating
    if place.ratings_total is not None:
        result["ratings_total"] = place.ratings_total
    if place.open_now is not None:
        result["open_now"] = place.open_now
    return result


async def google_places(ctx: ToolContext, query: str, near: str | None = None) -> dict[str, Any]:
    """Places Text Search around ``near``, or the saved address."""
    query = _text(query, "query", MAX_QUERY_CHARS)
    location = _text(near, "near", MAX_LOCATION_CHARS) if near else ctx.session.location
    if not location:
        return {
            "status": "need_location",
            "message": "No saved address. Ask the user where they are, then save it.",
        }
    try:
        places = await ctx.maps.text_search(f"{query} near {location}")
    except MapsError:
        return {"status": "error", "message": "Places search is unavailable right now."}
    ctx.session.places = places
    if not places:
        return {"status": "no_results", "query": query, "near": location}
    return {
        "status": "ok",
        "near": location,
        "results": [_result(number, place) for number, place in enumerate(places, start=1)],
    }


ASSISTANT_TOOLS: tuple[AssistantTool, ...] = (
    AssistantTool(
        name="set_user_location",
        description=(
            "Save the user's address so 'near me' searches use it. Call this whenever the "
            "user tells you where they are or where they live."
        ),
        parameters=SET_USER_LOCATION_PARAMETERS,
        handler=set_user_location,
    ),
    AssistantTool(
        name="google_places",
        description=(
            "Find real businesses with Google Places. ALWAYS use this to find or recommend a "
            "store, restaurant or service; never answer from memory. Results are numbered."
        ),
        parameters=GOOGLE_PLACES_PARAMETERS,
        handler=google_places,
    ),
)

ASSISTANT_TOOL_REGISTRY: dict[str, AssistantTool] = {tool.name: tool for tool in ASSISTANT_TOOLS}


def assistant_tool_schemas() -> list[dict[str, Any]]:
    return [tool.as_openai_schema() for tool in ASSISTANT_TOOLS]


async def dispatch(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    tool = ASSISTANT_TOOL_REGISTRY.get(name)
    if tool is None:
        raise ToolError(f"unknown tool: {name!r}")
    return await tool.handler(ctx, **arguments)
