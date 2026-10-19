"""[A3] Find stores near the user's saved address (set_user_location + google_places)."""

from __future__ import annotations

import asyncio
import json

import assistant_tools
import httpx
import pytest
import session
from assistant import ASSISTANT_PROMPT, Assistant, build_assistant, scrub_place_ids
from assistant_tools import ToolContext, google_places, set_user_location
from dispatcher import CallDispatcher
from fakes import ScriptedModel, call, say, tool_results
from fastapi.testclient import TestClient
from maps import GoogleMaps, MapsConfig, MapsError, MockMaps, Place, build_maps
from telephony import MockTelephony
from tools import ToolError
from webhook import SIGNATURE_HEADER, compute_signature, create_app
from whatsapp import MockWhatsApp

USER = "14155550100"
ADDRESS = "950 Main St, Worcester, MA"
KEY = "test-maps-key-0000"

PHARMACIES = [
    Place(
        "ChIJpharmacyAAAA0001", "Main Street Pharmacy", "12 Main St, Worcester, MA", 4.6, 212, True
    ),
    Place(
        "ChIJpharmacyBBBB0002", "Park Avenue Drug", "401 Park Ave, Worcester, MA", 4.1, 87, False
    ),
]


@pytest.fixture(autouse=True)
def _empty_sessions():
    session.SESSIONS.clear()
    yield
    session.SESSIONS.clear()


def _ctx(maps=None, location: str | None = ADDRESS) -> ToolContext:
    user = session.session_for(USER)
    user.location = location
    return ToolContext(user, maps or MockMaps(list(PHARMACIES)))


# --- Google Places client (HTTP mocked) -----------------------------------------


def _google(handler) -> GoogleMaps:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GoogleMaps(MapsConfig(api_key=KEY), http=http)


def _textsearch_body(status: str = "OK", results: list | None = None) -> dict:
    if results is None:
        results = [
            {
                "place_id": "ChIJpharmacyAAAA0001",
                "name": "Main Street Pharmacy",
                "formatted_address": "12 Main St, Worcester, MA",
                "rating": 4.6,
                "user_ratings_total": 212,
                "opening_hours": {"open_now": True},
            },
            {"place_id": "ChIJnoNameAAAA", "formatted_address": "nowhere"},
        ]
    return {"status": status, "results": results}


def test_text_search_queries_places_and_parses_results() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_textsearch_body())

    places = asyncio.run(_google(handler).text_search(f"pharmacy near {ADDRESS}"))

    assert seen[0].url.path == "/maps/api/place/textsearch/json"
    assert seen[0].url.params["query"] == f"pharmacy near {ADDRESS}"
    assert seen[0].url.params["key"] == KEY
    # The result without a name is dropped rather than shown half-empty.
    assert places == [PHARMACIES[0]]


def test_text_search_caps_results_at_five() -> None:
    results = [{"place_id": f"ChIJplace{i:04d}", "name": f"Store {i}"} for i in range(9)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_textsearch_body(results=results))

    assert len(asyncio.run(_google(handler).text_search("store"))) == 5


def test_text_search_zero_results_is_an_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_textsearch_body("ZERO_RESULTS", []))

    assert asyncio.run(_google(handler).text_search("unicorn store")) == []


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"status": "REQUEST_DENIED", "error_message": "bad key"}),
        httpx.Response(500, text="boom"),
    ],
)
def test_text_search_errors_never_carry_the_key(response: httpx.Response) -> None:
    with pytest.raises(MapsError) as excinfo:
        asyncio.run(_google(lambda request: response).text_search("pharmacy"))
    assert KEY not in str(excinfo.value)


def test_unreachable_maps_is_a_maps_error_without_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    with pytest.raises(MapsError) as excinfo:
        asyncio.run(_google(handler).text_search("pharmacy"))
    assert KEY not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


@pytest.mark.parametrize("key", ["", "your-google-maps-key-here"])
def test_build_maps_falls_back_to_the_mock_without_a_real_key(key: str) -> None:
    assert isinstance(build_maps(MapsConfig(api_key=key)), MockMaps)


def test_build_maps_uses_google_with_a_key() -> None:
    assert isinstance(build_maps(MapsConfig(api_key=KEY)), GoogleMaps)


def test_mock_maps_finds_nothing_unless_seeded() -> None:
    assert asyncio.run(MockMaps().text_search("pharmacy")) == []


# --- set_user_location -----------------------------------------------------------


def test_set_user_location_saves_the_address() -> None:
    ctx = _ctx(location=None)
    result = asyncio.run(set_user_location(ctx, address="  950 Main St,   Worcester, MA "))
    assert result == {"status": "saved", "location": ADDRESS}
    assert session.session_for(USER).location == ADDRESS


@pytest.mark.parametrize("address", ["", "   ", "x" * 201])
def test_set_user_location_rejects_blank_or_huge_addresses(address: str) -> None:
    with pytest.raises(ToolError):
        asyncio.run(set_user_location(_ctx(), address=address))


# --- google_places ------------------------------------------------------------------


def test_google_places_searches_near_the_saved_address() -> None:
    maps = MockMaps(list(PHARMACIES))
    result = asyncio.run(google_places(_ctx(maps), query="pharmacy"))

    assert maps.queries == [f"pharmacy near {ADDRESS}"]
    assert result["status"] == "ok"
    assert [r["name"] for r in result["results"]] == ["Main Street Pharmacy", "Park Avenue Drug"]
    assert result["results"][0] == {
        "result_number": 1,
        "name": "Main Street Pharmacy",
        "address": "12 Main St, Worcester, MA",
        "rating": 4.6,
        "ratings_total": 212,
        "open_now": True,
    }


def test_google_places_keeps_place_ids_out_of_the_result_but_in_the_session() -> None:
    result = asyncio.run(google_places(_ctx(), query="pharmacy"))

    assert "ChIJ" not in json.dumps(result)
    assert session.session_for(USER).place_ids == {p.place_id for p in PHARMACIES}


def test_google_places_honours_an_explicit_area() -> None:
    maps = MockMaps(list(PHARMACIES))
    asyncio.run(google_places(_ctx(maps), query="pharmacy", near="Back Bay, Boston"))
    assert maps.queries == ["pharmacy near Back Bay, Boston"]


def test_google_places_without_an_address_asks_for_one() -> None:
    maps = MockMaps(list(PHARMACIES))
    result = asyncio.run(google_places(_ctx(maps, location=None), query="pharmacy"))
    assert result["status"] == "need_location"
    assert maps.queries == []


def test_google_places_reports_no_results_honestly() -> None:
    result = asyncio.run(google_places(_ctx(MockMaps()), query="pharmacy"))
    assert result == {"status": "no_results", "query": "pharmacy", "near": ADDRESS}


def test_google_places_turns_an_api_failure_into_a_status() -> None:
    class Broken(MockMaps):
        async def text_search(self, query: str) -> list[Place]:
            raise MapsError("Google Maps error OVER_QUERY_LIMIT")

    assert asyncio.run(google_places(_ctx(Broken()), query="pharmacy"))["status"] == "error"


# --- the assistant loop -------------------------------------------------------------


def test_prompt_forbids_made_up_businesses() -> None:
    prompt = ASSISTANT_PROMPT.lower()
    assert "never make up business names, addresses, phone numbers, ratings" in prompt
    assert "must come from a google_places result" in prompt


def _answer_from_results(conversation: list[dict]) -> list:
    results = tool_results(conversation)[-1]["results"]
    lines = [f"{r['result_number']}. {r['name']} ({r['rating']}★)" for r in results]
    return say("Here's what I found near you:\n" + "\n".join(lines))


def test_find_a_pharmacy_near_me_returns_places_results() -> None:
    session.session_for(USER).location = ADDRESS
    model = ScriptedModel([[call("google_places", query="pharmacy")], _answer_from_results])
    maps = MockMaps(list(PHARMACIES))

    reply = asyncio.run(Assistant(model, maps).reply(USER, "find a pharmacy near me"))

    assert maps.queries == [f"pharmacy near {ADDRESS}"]
    assert "1. Main Street Pharmacy (4.6★)" in reply
    assert "2. Park Avenue Drug (4.1★)" in reply
    assert "ChIJ" not in reply
    assert {"set_user_location", "google_places"} <= {t["function"]["name"] for t in model.tools}


def test_saved_address_is_in_the_model_context() -> None:
    session.session_for(USER).location = ADDRESS
    model = ScriptedModel([say("Hi!")])
    asyncio.run(Assistant(model, MockMaps()).reply(USER, "hello"))
    assert ADDRESS in model.requests[0][0]["content"]


def test_location_then_search_in_one_message() -> None:
    model = ScriptedModel(
        [
            [call("set_user_location", address=ADDRESS)],
            [call("google_places", "call_2", query="pharmacy")],
            _answer_from_results,
        ]
    )
    maps = MockMaps(list(PHARMACIES))
    reply = asyncio.run(
        Assistant(model, maps).reply(USER, f"I'm at {ADDRESS}, find a pharmacy near me")
    )
    assert session.session_for(USER).location == ADDRESS
    assert maps.queries == [f"pharmacy near {ADDRESS}"]
    assert "Main Street Pharmacy" in reply


def test_a_leaked_place_id_is_scrubbed_from_the_reply() -> None:
    session.session_for(USER).location = ADDRESS
    model = ScriptedModel(
        [
            [call("google_places", query="pharmacy")],
            say("1. Main Street Pharmacy [place_id:ChIJpharmacyAAAA0001] is open now."),
        ]
    )
    reply = asyncio.run(Assistant(model, MockMaps(list(PHARMACIES))).reply(USER, "pharmacy?"))
    assert reply == "1. Main Street Pharmacy is open now."


def test_scrub_removes_known_ids_and_id_shaped_strings() -> None:
    text = "A (abc123known) and ChIJ0123456789abcdef and place_id=XYZ."
    assert scrub_place_ids(text, {"abc123known"}) == "A () and and."


def test_bad_tool_arguments_go_back_to_the_model_as_an_error() -> None:
    model = ScriptedModel([[call("set_user_location", address="")], say("Where are you?")])
    reply = asyncio.run(Assistant(model, MockMaps()).reply(USER, "near me"))
    assert reply == "Where are you?"
    assert tool_results(model.requests[1])[0]["status"] == "error"


def test_unknown_tool_is_reported_not_raised() -> None:
    model = ScriptedModel([[call("launch_missiles")], say("I can't do that.")])
    assert asyncio.run(Assistant(model, MockMaps()).reply(USER, "x")) == "I can't do that."


def test_runaway_tool_loop_is_bounded() -> None:
    model = ScriptedModel([[call("google_places", query="pharmacy")]] * 3)
    session.session_for(USER).location = ADDRESS
    reply = asyncio.run(Assistant(model, MockMaps(), max_rounds=3).reply(USER, "pharmacy"))
    assert reply.startswith("Sorry")


def test_history_carries_into_the_next_message() -> None:
    model = ScriptedModel([say("Where are you?"), say("Got it.")])
    bot = Assistant(model, MockMaps())
    asyncio.run(bot.reply(USER, "find a pharmacy"))
    asyncio.run(bot.reply(USER, "Worcester"))
    roles = [m["role"] for m in model.requests[1]]
    assert roles == ["system", "user", "assistant", "user"]


def test_assistant_is_off_without_an_openai_key() -> None:
    assert build_assistant() is None


def test_assistant_tool_registry_matches_the_tuple() -> None:
    assert set(assistant_tools.ASSISTANT_TOOL_REGISTRY) == {
        t.name for t in assistant_tools.ASSISTANT_TOOLS
    }


# --- through the webhook --------------------------------------------------------------


def _post(client: TestClient, text: str):
    body = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": USER,
                                        "id": "wamid.1",
                                        "type": "text",
                                        "text": {"body": text},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    headers = {SIGNATURE_HEADER: compute_signature(body), "Content-Type": "application/json"}
    return client.post("/webhook/whatsapp", content=body, headers=headers)


def test_webhook_routes_a_search_to_the_assistant() -> None:
    session.session_for(USER).location = ADDRESS
    outbox = MockWhatsApp()
    model = ScriptedModel([[call("google_places", query="pharmacy")], _answer_from_results])
    app = create_app(
        whatsapp=outbox,
        dialer=CallDispatcher(MockTelephony(), "k"),
        assistant=Assistant(model, MockMaps(list(PHARMACIES))),
    )
    with TestClient(app) as client:
        response = _post(client, "find a pharmacy near me")

    assert response.json()["status"] == "assistant_replied"
    [reply] = outbox.texts_to(USER)
    assert "Main Street Pharmacy" in reply and "ChIJ" not in reply


def test_webhook_keeps_rule_based_intake_for_instructions_with_a_number() -> None:
    outbox = MockWhatsApp()
    model = ScriptedModel([])
    app = create_app(
        whatsapp=outbox,
        dialer=CallDispatcher(MockTelephony(), "k"),
        assistant=Assistant(model, MockMaps()),
    )
    with TestClient(app) as client:
        response = _post(client, "Call Bella Vista on +1 415 555 0123 and book a table for 4")

    assert response.json()["status"] == "accepted"
    assert model.requests == []
    assert outbox.texts_to(USER)[0].startswith("Before I call")
