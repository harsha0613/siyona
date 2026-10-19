"""Tools the WhatsApp assistant's model may call.

Same contract as the call-time tools in ``tools.py`` (JSON Schema arguments, a
handler returning a JSON-serialisable dict), with two differences: handlers are
async, and each receives a :class:`ToolContext` holding the user's session, the
Maps client and the confirmation gate.

``create_call_request`` never takes a phone number from the model. It dials the
number Place Details returned for the place last looked up, and only after the
user has said YES to the confirmation summary.

Place ids stay out of the model's view entirely. ``google_places`` returns
numbered results and keeps the ids in the session, so the model can only refer
to a place it was actually shown, and has nothing to leak into a reply.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

from maps import Maps, MapsError, Place, PlaceDetails
from phone import PhoneNumberError, find_numbers, region_of, whatsapp_to_e164
from session import Session
from state_machine import MAX_ATTEMPTS_PER_NUMBER
from tasks import Task, create_task
from tools import Tool, ToolError

MAX_LOCATION_CHARS = 200
MAX_QUERY_CHARS = 120


class Confirmer(Protocol):
    async def ask_to_confirm(self, task: Task) -> None:
        """Send the who / what / when summary; nothing is dialled until YES."""
        ...


@dataclass
class ToolContext:
    session: Session
    maps: Maps
    gate: Confirmer | None = None
    # Set when a tool sent the user a confirmation summary during this turn.
    confirmation_opened: bool = False


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


# ---------------------------------------------------------------------------
# place_details
# ---------------------------------------------------------------------------

NOT_AVAILABLE = "Not available"
MAX_REVIEW_CHARS = 200
_PRICE_LEVELS = {0: "Free", 1: "$", 2: "$$", 3: "$$$", 4: "$$$$"}

PLACE_DETAILS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "place": {
            "type": "string",
            "description": (
                "Which place from the last google_places search: its result number "
                "(e.g. '1' for 'the first one') or its name as listed."
            ),
            "maxLength": MAX_QUERY_CHARS,
        },
    },
    "required": ["place"],
    "additionalProperties": False,
}

_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": -1}


def resolve_place(session: Session, place: str) -> Place | None:
    """The place from the last search that ``place`` refers to, if exactly one matches."""
    wanted = " ".join(place.lower().split())
    exact = [p for p in session.places if p.name.lower() == wanted]
    if exact:
        return exact[0]
    words = wanted.replace("#", " ").split()
    number = next((int(w) for w in words if w.isdigit()), None)
    if number is None:
        number = next((_ORDINALS[w] for w in words if w in _ORDINALS), None)
    if number is not None:
        if number == -1:
            return session.places[-1] if session.places else None
        return session.places[number - 1] if 1 <= number <= len(session.places) else None
    partial = [p for p in session.places if wanted in p.name.lower()]
    return partial[0] if len(partial) == 1 else None


def _or_not_available(value: Any) -> Any:
    return NOT_AVAILABLE if value in (None, "", ()) else value


def details_result(details: PlaceDetails) -> dict[str, Any]:
    """What the model is shown. Missing fields say "Not available", never a guess."""
    return {
        "status": "ok",
        "name": details.name,
        "address": _or_not_available(details.address),
        "phone": _or_not_available(details.phone),
        "website": _or_not_available(details.website),
        "opening_hours": list(details.opening_hours) or NOT_AVAILABLE,
        "rating": _or_not_available(details.rating),
        "price_level": _PRICE_LEVELS.get(details.price_level, NOT_AVAILABLE),
        "reviews": [
            {
                "author": review.author,
                "rating": _or_not_available(review.rating),
                "text": review.text[:MAX_REVIEW_CHARS],
            }
            for review in details.reviews
        ]
        or NOT_AVAILABLE,
        "maps_url": _or_not_available(details.maps_url),
    }


async def place_details(ctx: ToolContext, place: str) -> dict[str, Any]:
    """Phone, address, website, hours, rating, price level, reviews and Maps link."""
    place = _text(place, "place", MAX_QUERY_CHARS)
    found = resolve_place(ctx.session, place)
    if found is None:
        return {
            "status": "not_found",
            "message": f"No place in the last search matches {place!r}. Search first or ask.",
        }
    try:
        details = await ctx.maps.place_details(found.place_id)
    except MapsError:
        return {"status": "error", "message": "Place details are unavailable right now."}
    if not details.name:
        details = replace(details, name=found.name)
    ctx.session.current_place = details
    return details_result(details)


# ---------------------------------------------------------------------------
# create_call_request
# ---------------------------------------------------------------------------

MAX_TASK_CHARS = 300
MAX_DETAILS_CHARS = 1000

CREATE_CALL_REQUEST_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {
            "type": "string",
            "description": (
                "What to get done on the call, in one line, e.g. 'Ask whether they have "
                "amoxicillin 500 mg in stock and hold it for pickup today'."
            ),
            "maxLength": MAX_TASK_CHARS,
        },
        "details": {
            "type": "string",
            "description": (
                "Everything the store may need, as the user gave it: the customer's name, "
                "the item or service, dates and times, preferences. Never invent any of it."
            ),
            "maxLength": MAX_DETAILS_CHARS,
        },
        "questions_asked": {
            "type": "boolean",
            "description": (
                "True once you have asked the user the 2-3 most important questions for this "
                "call and have their answers, or they told you to just call."
            ),
        },
    },
    "required": ["task", "details", "questions_asked"],
    "additionalProperties": False,
}


def max_attempts_from_env() -> int:
    return int(os.environ.get("MAX_ATTEMPTS_PER_NUMBER", MAX_ATTEMPTS_PER_NUMBER))


def dialable_number(details: PlaceDetails, user_phone: str) -> str | None:
    """The E.164 number Google lists for the place, or None. Never anything else."""
    try:
        region = region_of(whatsapp_to_e164(user_phone))
    except PhoneNumberError:
        region = None
    for listed in (details.international_phone, details.phone):
        if listed:
            numbers = find_numbers(listed, region)
            if numbers:
                return numbers[0]
    return None


async def create_call_request(
    ctx: ToolContext, task: str, details: str, questions_asked: bool
) -> dict[str, Any]:
    """Prepare a call to the place last looked up and ask the user to confirm it."""
    goal = _text(task, "task", MAX_TASK_CHARS)
    details = _text(details, "details", MAX_DETAILS_CHARS)
    if questions_asked is not True:
        return {
            "status": "ask_first",
            "message": (
                "Ask the user the 2-3 most important questions for this call in one message, "
                "then call create_call_request with their answers."
            ),
        }
    place = ctx.session.current_place
    if place is None:
        return {"status": "need_place", "message": "Look the store up with place_details first."}
    number = dialable_number(place, ctx.session.user_phone)
    if number is None:
        return {
            "status": "no_phone",
            "message": f"Google has no phone number for {place.name}, so it can't be called.",
        }
    if ctx.gate is None:
        return {"status": "error", "message": "Calling isn't available right now."}

    session = ctx.session
    call = create_task(session.user_phone, goal)
    call.goal, call.callee_name, call.numbers = goal, place.name, [number]
    call.details, call.place_id = details, place.place_id
    call.user_name, call.user_location = session.user_name, session.location
    call.max_attempts = max(1, max_attempts_from_env())
    if session.last_call_place_id == place.place_id:
        call.follow_up_of = session.last_call_task_id
    await ctx.gate.ask_to_confirm(call)
    ctx.confirmation_opened = True
    return {
        "status": "awaiting_confirmation",
        "message": "The user was sent the call summary and must reply YES before it is dialled.",
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
    AssistantTool(
        name="place_details",
        description=(
            "Get a found place's phone number, address, website, opening hours, rating, price "
            "level, reviews and Google Maps link. Use for 'the first one', 'tell me more about "
            "X', or any question about a place from the last search."
        ),
        parameters=PLACE_DETAILS_PARAMETERS,
        handler=place_details,
    ),
    AssistantTool(
        name="create_call_request",
        description=(
            "Call the store from the last place_details lookup on the user's behalf. Only use "
            "it after asking the 2-3 key questions for the call. The user is sent a summary "
            "and nothing is dialled until they reply YES."
        ),
        parameters=CREATE_CALL_REQUEST_PARAMETERS,
        handler=create_call_request,
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
