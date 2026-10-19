"""[A3] Directions and travel time to a store (get_directions)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import session
from assistant import ASSISTANT_PROMPT, Assistant
from assistant_tools import GET_DIRECTIONS_PARAMETERS, ToolContext, get_directions
from fakes import ScriptedModel, call, say, tool_results
from maps import (
    TRAVEL_MODES,
    GoogleMaps,
    MapsConfig,
    MapsError,
    MockMaps,
    Place,
    PlaceDetails,
    Route,
    RouteStep,
)
from tools import ToolError

USER = "14155550100"
ADDRESS = "950 Main St, Worcester, MA"
KEY = "test-maps-key-0000"

PLACES = [
    Place("ChIJpharmacyAAAA0001", "Main Street Pharmacy", "12 Main St, Worcester, MA"),
    Place("ChIJpharmacyBBBB0002", "Park Avenue Drug", "401 Park Ave, Worcester, MA"),
]
STORE = PlaceDetails("ChIJpharmacyAAAA0001", "Main Street Pharmacy", "12 Main St, Worcester, MA")

DRIVE = Route(
    mode="driving",
    distance="1.2 mi",
    duration="6 mins",
    start_address="950 Main St, Worcester, MA 01610, USA",
    end_address="12 Main St, Worcester, MA 01608, USA",
    duration_in_traffic="9 mins",
    steps=(RouteStep("Head north on Main St", "0.4 mi", "2 mins"), RouteStep("Turn right")),
)
WALK = Route(mode="walking", distance="1.1 mi", duration="23 mins")


@pytest.fixture(autouse=True)
def _empty_sessions():
    session.SESSIONS.clear()
    yield
    session.SESSIONS.clear()


def _maps() -> MockMaps:
    return MockMaps(list(PLACES), routes={"driving": DRIVE, "walking": WALK})


def _ctx(maps: MockMaps | None = None, location: str | None = ADDRESS) -> ToolContext:
    user = session.session_for(USER)
    user.location, user.places, user.current_place = location, list(PLACES), STORE
    return ToolContext(user, maps or _maps())


def _run(ctx: ToolContext, **arguments) -> dict:
    return asyncio.run(get_directions(ctx, **arguments))


# --- Directions client (HTTP mocked) ------------------------------------------------


def _google(handler) -> GoogleMaps:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GoogleMaps(MapsConfig(api_key=KEY), http=http)


DIRECTIONS_BODY = {
    "status": "OK",
    "routes": [
        {
            "legs": [
                {
                    "distance": {"text": "1.2 mi", "value": 1931},
                    "duration": {"text": "6 mins", "value": 360},
                    "duration_in_traffic": {"text": "9 mins", "value": 540},
                    "start_address": "950 Main St, Worcester, MA 01610, USA",
                    "end_address": "12 Main St, Worcester, MA 01608, USA",
                    "steps": [
                        {
                            "html_instructions": "Head <b>north</b> on <b>Main St</b>",
                            "distance": {"text": "0.4 mi"},
                            "duration": {"text": "2 mins"},
                        },
                        {
                            "html_instructions": (
                                'Turn <b>right</b><div style="font-size:0.9em">'
                                "Destination will be on the left &amp; ahead</div>"
                            ),
                            "distance": {"text": "0.8 mi"},
                            "duration": {"text": "4 mins"},
                        },
                    ]
                    + [{"html_instructions": f"Step {i}"} for i in range(3, 9)],
                }
            ]
        }
    ],
}


def test_directions_request_and_parsing() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DIRECTIONS_BODY)

    route = asyncio.run(
        _google(handler).directions(ADDRESS, "place_id:ChIJpharmacyAAAA0001", "driving")
    )

    params = seen[0].url.params
    assert seen[0].url.path == "/maps/api/directions/json"
    assert params["origin"] == ADDRESS
    assert params["destination"] == "place_id:ChIJpharmacyAAAA0001"
    assert params["mode"] == "driving" and params["departure_time"] == "now"
    assert params["key"] == KEY
    assert route is not None
    assert (route.distance, route.duration, route.duration_in_traffic) == (
        "1.2 mi",
        "6 mins",
        "9 mins",
    )
    assert route.steps[0] == RouteStep("Head north on Main St", "0.4 mi", "2 mins")
    assert route.steps[1].instruction == "Turn right Destination will be on the left & ahead"
    assert len(route.steps) == 5


@pytest.mark.parametrize(
    ("mode", "departure"), [("walking", False), ("bicycling", False), ("transit", True)]
)
def test_each_mode_is_sent_to_google(mode: str, departure: bool) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=DIRECTIONS_BODY)

    asyncio.run(_google(handler).directions(ADDRESS, "Boston", mode))
    assert seen[0].url.params["mode"] == mode
    assert ("departure_time" in seen[0].url.params) is departure


@pytest.mark.parametrize("status", ["ZERO_RESULTS", "NOT_FOUND"])
def test_no_route_is_none(status: str) -> None:
    body = {"status": status, "routes": []}
    route = asyncio.run(
        _google(lambda r: httpx.Response(200, json=body)).directions(ADDRESS, "Atlantis", "driving")
    )
    assert route is None


def test_directions_errors_never_carry_the_key() -> None:
    body = {"status": "REQUEST_DENIED", "error_message": "API key invalid"}
    with pytest.raises(MapsError) as excinfo:
        asyncio.run(
            _google(lambda r: httpx.Response(200, json=body)).directions(ADDRESS, "x", "driving")
        )
    assert KEY not in str(excinfo.value)


def test_client_refuses_an_unknown_mode() -> None:
    with pytest.raises(MapsError):
        asyncio.run(_google(lambda r: httpx.Response(200)).directions(ADDRESS, "x", "teleport"))


# --- get_directions tool --------------------------------------------------------------


def test_how_long_to_get_there_uses_the_saved_address_and_driving() -> None:
    maps = _maps()
    result = _run(_ctx(maps), destination="there")

    assert maps.route_requests == [(ADDRESS, "place_id:ChIJpharmacyAAAA0001", "driving")]
    assert result == {
        "status": "ok",
        "from": "950 Main St, Worcester, MA 01610, USA",
        "to": "Main Street Pharmacy",
        "mode": "driving",
        "distance": "1.2 mi",
        "eta": "6 mins",
        "eta_in_traffic": "9 mins",
        "steps": ["Head north on Main St (0.4 mi)", "Turn right"],
    }


def test_a_named_mode_is_honoured() -> None:
    maps = _maps()
    result = _run(_ctx(maps), destination="there", mode="walking")
    assert maps.route_requests[0][2] == "walking"
    assert (result["mode"], result["distance"], result["eta"]) == ("walking", "1.1 mi", "23 mins")
    assert "eta_in_traffic" not in result


def test_mode_enum_matches_the_supported_modes() -> None:
    assert GET_DIRECTIONS_PARAMETERS["properties"]["mode"]["enum"] == list(TRAVEL_MODES)
    assert TRAVEL_MODES == ("driving", "walking", "transit", "bicycling")


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ToolError):
        _run(_ctx(), destination="there", mode="teleport")


def test_a_place_from_the_last_search_goes_by_its_place_id() -> None:
    maps = _maps()
    result = _run(_ctx(maps), destination="Park Avenue Drug")
    assert maps.route_requests[0][1] == "place_id:ChIJpharmacyBBBB0002"
    assert result["to"] == "Park Avenue Drug"
    assert "ChIJ" not in json.dumps(result)


def test_any_address_and_an_explicit_origin_work_too() -> None:
    maps = _maps()
    result = _run(_ctx(maps), destination="Union Station, Worcester", origin="Clark University")
    assert maps.route_requests == [("Clark University", "Union Station, Worcester", "driving")]
    assert result["status"] == "ok"


def test_without_a_saved_address_ask_where_to_start() -> None:
    maps = _maps()
    assert _run(_ctx(maps, location=None), destination="there")["status"] == "need_location"
    assert maps.route_requests == []


def test_there_with_nothing_discussed_asks_where_to() -> None:
    ctx = _ctx()
    ctx.session.current_place = None
    assert _run(ctx, destination="there")["status"] == "need_destination"


def test_no_route_is_reported() -> None:
    result = _run(_ctx(_maps()), destination="there", mode="transit")
    assert result == {
        "status": "no_route",
        "from": ADDRESS,
        "to": "Main Street Pharmacy",
        "mode": "transit",
    }


def test_api_failure_becomes_a_status() -> None:
    class Broken(MockMaps):
        async def directions(self, origin: str, destination: str, mode: str):
            raise MapsError("OVER_QUERY_LIMIT")

    assert _run(_ctx(Broken()), destination="there")["status"] == "error"


# --- through the assistant ------------------------------------------------------------------


def test_prompt_defaults_to_driving_from_the_saved_address() -> None:
    prompt = ASSISTANT_PROMPT.lower()
    assert "call get_directions" in prompt
    assert "starts from the saved address" in prompt
    assert "use driving unless the user names walking, transit or bicycling" in prompt


def test_how_long_to_get_there_through_the_assistant() -> None:
    _ctx()

    def answer(conversation: list[dict]) -> list:
        route = tool_results(conversation)[-1]
        return say(f"{route['to']} is {route['distance']} away, about {route['eta']} by car.")

    model = ScriptedModel([[call("get_directions", destination="there")], answer])
    reply = asyncio.run(Assistant(model, _maps()).reply(USER, "how long to get there?"))
    assert reply == "Main Street Pharmacy is 1.2 mi away, about 6 mins by car."
