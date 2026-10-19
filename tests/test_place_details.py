"""[A3] Get a found store's phone number, hours and reviews (place_details)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import session
from assistant import ASSISTANT_PROMPT, Assistant
from assistant_tools import NOT_AVAILABLE, ToolContext, place_details, resolve_place
from fakes import ScriptedModel, call, say, tool_results
from maps import GoogleMaps, MapsConfig, MapsError, MockMaps, Place, PlaceDetails, Review
from tools import ToolError

USER = "14155550100"
KEY = "test-maps-key-0000"

PLACES = [
    Place("ChIJpharmacyAAAA0001", "Main Street Pharmacy", "12 Main St, Worcester, MA", 4.6),
    Place("ChIJpharmacyBBBB0002", "Park Avenue Drug", "401 Park Ave, Worcester, MA", 4.1),
    Place("ChIJpharmacyCCCC0003", "CVS Pharmacy", "1 Front St, Worcester, MA", 3.9),
]

MAIN_STREET = PlaceDetails(
    place_id="ChIJpharmacyAAAA0001",
    name="Main Street Pharmacy",
    address="12 Main St, Worcester, MA 01608, USA",
    phone="(508) 555-0142",
    website="https://mainstreetpharmacy.example",
    opening_hours=("Monday: 8:00 AM – 9:00 PM", "Sunday: Closed"),
    rating=4.6,
    price_level=2,
    reviews=(Review("A. Customer", 5, "Quick and friendly."),),
    maps_url="https://maps.google.com/?cid=1",
)
NO_PHONE = PlaceDetails(place_id="ChIJpharmacyBBBB0002", name="Park Avenue Drug")


@pytest.fixture(autouse=True)
def _empty_sessions():
    session.SESSIONS.clear()
    yield
    session.SESSIONS.clear()


def _maps() -> MockMaps:
    return MockMaps(list(PLACES), {d.place_id: d for d in (MAIN_STREET, NO_PHONE)})


def _ctx(maps: MockMaps | None = None) -> ToolContext:
    user = session.session_for(USER)
    user.places = list(PLACES)
    return ToolContext(user, maps or _maps())


# --- Place Details client (HTTP mocked) ------------------------------------------


def _google(handler) -> GoogleMaps:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GoogleMaps(MapsConfig(api_key=KEY), http=http)


DETAILS_BODY = {
    "status": "OK",
    "result": {
        "place_id": "ChIJpharmacyAAAA0001",
        "name": "Main Street Pharmacy",
        "formatted_address": "12 Main St, Worcester, MA 01608, USA",
        "formatted_phone_number": "(508) 555-0142",
        "website": "https://mainstreetpharmacy.example",
        "opening_hours": {"weekday_text": ["Monday: 8:00 AM – 9:00 PM", "Sunday: Closed"]},
        "rating": 4.6,
        "price_level": 2,
        "reviews": [
            {"author_name": "A. Customer", "rating": 5, "text": "Quick and friendly."},
            {"author_name": "B", "rating": 4, "text": "ok"},
            {"author_name": "C", "rating": 3, "text": "ok"},
            {"author_name": "D", "rating": 2, "text": "ok"},
        ],
        "url": "https://maps.google.com/?cid=1",
    },
}


def test_place_details_requests_the_fields_and_parses_them() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DETAILS_BODY)

    details = asyncio.run(_google(handler).place_details("ChIJpharmacyAAAA0001"))

    params = seen[0].url.params
    assert seen[0].url.path == "/maps/api/place/details/json"
    assert params["place_id"] == "ChIJpharmacyAAAA0001"
    for field in ("formatted_phone_number", "opening_hours", "reviews", "website", "url"):
        assert field in params["fields"]
    assert details.phone == "(508) 555-0142"
    assert details.opening_hours == MAIN_STREET.opening_hours
    assert len(details.reviews) == 3
    assert details.reviews[0] == MAIN_STREET.reviews[0]
    assert details.maps_url == MAIN_STREET.maps_url


def test_place_details_without_a_phone_parses_as_none() -> None:
    body = {"status": "OK", "result": {"place_id": "ChIJx", "name": "Park Avenue Drug"}}
    details = asyncio.run(
        _google(lambda request: httpx.Response(200, json=body)).place_details("ChIJx")
    )
    assert details.phone is None and details.opening_hours == ()


@pytest.mark.parametrize(
    "body", [{"status": "NOT_FOUND"}, {"status": "INVALID_REQUEST"}, {"status": "OK"}]
)
def test_place_details_errors_are_maps_errors_without_the_key(body: dict) -> None:
    with pytest.raises(MapsError) as excinfo:
        asyncio.run(_google(lambda r: httpx.Response(200, json=body)).place_details("ChIJx"))
    assert KEY not in str(excinfo.value)


# --- resolving "the first one" / "tell me more about X" --------------------------


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("1", 0),
        ("#2", 1),
        ("the first one", 0),
        ("second", 1),
        ("the last one", 2),
        ("Main Street Pharmacy", 0),
        ("park avenue", 1),
        ("cvs", 2),
    ],
)
def test_resolve_place_by_number_ordinal_or_name(phrase: str, expected: int) -> None:
    user = _ctx().session
    assert resolve_place(user, phrase) == PLACES[expected]


@pytest.mark.parametrize("phrase", ["7", "pharmacy", "Walgreens", "the fourth one"])
def test_resolve_place_refuses_unknown_or_ambiguous(phrase: str) -> None:
    # "pharmacy" matches two names, so it is ambiguous rather than a guess.
    assert resolve_place(_ctx().session, phrase) is None


# --- place_details tool -------------------------------------------------------------


def test_phone_and_hours_for_the_chosen_store() -> None:
    maps = _maps()
    result = asyncio.run(place_details(_ctx(maps), place="the first one"))

    assert maps.detail_requests == ["ChIJpharmacyAAAA0001"]
    assert result == {
        "status": "ok",
        "name": "Main Street Pharmacy",
        "address": "12 Main St, Worcester, MA 01608, USA",
        "phone": "(508) 555-0142",
        "website": "https://mainstreetpharmacy.example",
        "opening_hours": ["Monday: 8:00 AM – 9:00 PM", "Sunday: Closed"],
        "rating": 4.6,
        "price_level": "$$",
        "reviews": [{"author": "A. Customer", "rating": 5, "text": "Quick and friendly."}],
        "maps_url": "https://maps.google.com/?cid=1",
    }
    assert session.session_for(USER).current_place == MAIN_STREET


def test_a_store_with_no_phone_says_not_available() -> None:
    result = asyncio.run(place_details(_ctx(), place="Park Avenue Drug"))
    assert result["phone"] == NOT_AVAILABLE
    for field in ("address", "website", "opening_hours", "rating", "price_level", "reviews"):
        assert result[field] == NOT_AVAILABLE


def test_place_ids_never_reach_the_model() -> None:
    result = asyncio.run(place_details(_ctx(), place="1"))
    assert "ChIJ" not in json.dumps(result)


def test_unknown_place_is_not_found_and_nothing_is_fetched() -> None:
    maps = _maps()
    result = asyncio.run(place_details(_ctx(maps), place="Walgreens"))
    assert result["status"] == "not_found"
    assert maps.detail_requests == []


def test_without_a_search_there_is_nothing_to_resolve() -> None:
    ctx = ToolContext(session.session_for(USER), _maps())
    assert asyncio.run(place_details(ctx, place="1"))["status"] == "not_found"


def test_api_failure_becomes_a_status() -> None:
    maps = MockMaps(list(PLACES))  # no details seeded: the mock raises MapsError
    assert asyncio.run(place_details(_ctx(maps), place="1"))["status"] == "error"


def test_long_reviews_are_trimmed() -> None:
    long = PlaceDetails("ChIJpharmacyAAAA0001", "Main", reviews=(Review("X", 4, "y" * 900),))
    maps = MockMaps(list(PLACES), {long.place_id: long})
    result = asyncio.run(place_details(_ctx(maps), place="1"))
    assert len(result["reviews"][0]["text"]) == 200


def test_blank_place_is_rejected() -> None:
    with pytest.raises(ToolError):
        asyncio.run(place_details(_ctx(), place="  "))


# --- through the assistant ------------------------------------------------------------


def test_prompt_says_never_fill_in_missing_fields() -> None:
    assert 'if a field is "not available", say it is not available' in ASSISTANT_PROMPT.lower()


def test_tell_me_more_about_the_first_one() -> None:
    session.session_for(USER).places = list(PLACES)

    def answer(conversation: list[dict]) -> list:
        details = tool_results(conversation)[-1]
        return say(f"{details['name']}: {details['phone']}. {details['opening_hours'][0]}")

    model = ScriptedModel([[call("place_details", place="the first one")], answer])
    reply = asyncio.run(Assistant(model, _maps()).reply(USER, "tell me more about the first one"))
    assert reply == "Main Street Pharmacy: (508) 555-0142. Monday: 8:00 AM – 9:00 PM"


def test_no_phone_reaches_the_user_as_not_available() -> None:
    session.session_for(USER).places = list(PLACES)

    def answer(conversation: list[dict]) -> list:
        return say(f"Phone: {tool_results(conversation)[-1]['phone']}")

    model = ScriptedModel([[call("place_details", place="2")], answer])
    reply = asyncio.run(Assistant(model, _maps()).reply(USER, "what's their number?"))
    assert reply == "Phone: Not available"
