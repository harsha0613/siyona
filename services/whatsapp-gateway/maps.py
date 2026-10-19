"""Google Maps Platform boundary.

The assistant only ever talks to the :class:`Maps` protocol, so the vendor is
swappable and tests never touch the network:

- :class:`GoogleMaps` — the Places Text Search, Place Details and Directions
  web services over httpx.
- :class:`MockMaps` — in-memory, returns only the places it was given. It is the
  default whenever ``GOOGLE_MAPS_API_KEY`` is unset or a placeholder, so local
  runs and CI need no key. With no places seeded it finds nothing: a keyless run
  never shows a business that doesn't exist.

The API key travels as a query parameter, so it is never logged and never put
into an error message.
"""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger("siyona.gateway.maps")

PLACES_BASE_URL = "https://maps.googleapis.com/maps/api/place"
DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"
TRAVEL_MODES = ("driving", "walking", "transit", "bicycling")
MAX_STEPS = 5
MAX_RESULTS = 5
MAX_REVIEWS = 3
DETAILS_FIELDS = (
    "place_id,name,formatted_address,formatted_phone_number,international_phone_number,"
    "website,opening_hours,"
    "rating,price_level,reviews,url"
)


class MapsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Place:
    """One Places search result, as much as the assistant needs of it."""

    place_id: str
    name: str
    address: str
    rating: float | None = None
    ratings_total: int | None = None
    open_now: bool | None = None


@dataclass(frozen=True)
class Review:
    author: str
    rating: int | None
    text: str


@dataclass(frozen=True)
class PlaceDetails:
    """Place Details for one place. Any field Google doesn't have is None or empty."""

    place_id: str
    name: str
    address: str | None = None
    phone: str | None = None
    # Same number with the country code ("+1 508-555-0142"): what a call dials.
    international_phone: str | None = None
    website: str | None = None
    opening_hours: tuple[str, ...] = ()
    rating: float | None = None
    price_level: int | None = None
    reviews: tuple[Review, ...] = ()
    maps_url: str | None = None


@dataclass(frozen=True)
class RouteStep:
    instruction: str
    distance: str | None = None
    duration: str | None = None


@dataclass(frozen=True)
class Route:
    """The first leg of the best route Google returned."""

    mode: str
    distance: str
    duration: str
    start_address: str | None = None
    end_address: str | None = None
    # Driving only, when Google has live traffic for the trip.
    duration_in_traffic: str | None = None
    steps: tuple[RouteStep, ...] = ()


class Maps(Protocol):
    async def text_search(self, query: str) -> list[Place]:
        """Places matching ``query`` (e.g. "pharmacy near 1 Main St, Worcester MA")."""
        ...

    async def place_details(self, place_id: str) -> PlaceDetails:
        """Phone, hours, website, reviews and so on for one place."""
        ...

    async def directions(self, origin: str, destination: str, mode: str) -> Route | None:
        """Distance, ETA and steps from ``origin`` to ``destination``; None if no route.

        Either end may be an address, a place name, or ``place_id:<id>``.
        """
        ...


@dataclass(frozen=True)
class MapsConfig:
    api_key: str = ""

    @classmethod
    def from_env(cls) -> MapsConfig:
        return cls(api_key=os.environ.get("GOOGLE_MAPS_API_KEY", ""))

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("your-")


def _place(result: dict[str, Any]) -> Place | None:
    place_id, name = result.get("place_id"), result.get("name")
    if not place_id or not name:
        return None
    hours = result.get("opening_hours")
    return Place(
        place_id=str(place_id),
        name=str(name),
        address=str(result.get("formatted_address") or ""),
        rating=result.get("rating"),
        ratings_total=result.get("user_ratings_total"),
        open_now=hours.get("open_now") if isinstance(hours, dict) else None,
    )


def _details(place_id: str, result: dict[str, Any]) -> PlaceDetails:
    hours = result.get("opening_hours")
    weekday_text = hours.get("weekday_text") if isinstance(hours, dict) else None
    reviews = [
        Review(
            author=str(review.get("author_name") or "Anonymous"),
            rating=review.get("rating"),
            text=str(review.get("text") or ""),
        )
        for review in (result.get("reviews") or [])[:MAX_REVIEWS]
        if isinstance(review, dict)
    ]
    return PlaceDetails(
        place_id=str(result.get("place_id") or place_id),
        name=str(result.get("name") or ""),
        address=result.get("formatted_address") or None,
        phone=result.get("formatted_phone_number") or None,
        international_phone=result.get("international_phone_number") or None,
        website=result.get("website") or None,
        opening_hours=tuple(str(day) for day in weekday_text or ()),
        rating=result.get("rating"),
        price_level=result.get("price_level"),
        reviews=tuple(reviews),
        maps_url=result.get("url") or None,
    )


_TAG = re.compile(r"<[^>]+>")


def _plain(instruction: str) -> str:
    """Google's step instructions are HTML; WhatsApp wants plain text."""
    text = _TAG.sub(" ", instruction.replace("<div", " <div"))
    return " ".join(html.unescape(text).split())


def _text_of(value: Any) -> str | None:
    return value.get("text") or None if isinstance(value, dict) else None


def _route(mode: str, body: dict[str, Any]) -> Route | None:
    routes = body.get("routes") or []
    legs = routes[0].get("legs") if routes and isinstance(routes[0], dict) else None
    if not legs:
        return None
    leg = legs[0]
    distance, duration = _text_of(leg.get("distance")), _text_of(leg.get("duration"))
    if not distance or not duration:
        return None
    steps = tuple(
        RouteStep(
            instruction=_plain(str(step.get("html_instructions") or "")),
            distance=_text_of(step.get("distance")),
            duration=_text_of(step.get("duration")),
        )
        for step in (leg.get("steps") or [])[:MAX_STEPS]
        if isinstance(step, dict) and step.get("html_instructions")
    )
    return Route(
        mode=mode,
        distance=distance,
        duration=duration,
        start_address=leg.get("start_address") or None,
        end_address=leg.get("end_address") or None,
        duration_in_traffic=_text_of(leg.get("duration_in_traffic")),
        steps=steps,
    )


class GoogleMaps:
    def __init__(self, config: MapsConfig, http: httpx.AsyncClient | None = None) -> None:
        self._key = config.api_key
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))

    async def _get(
        self, path: str, params: dict[str, str], ok: tuple[str, ...] = ("OK", "ZERO_RESULTS")
    ) -> dict[str, Any]:
        url = path if path.startswith("https://") else f"{PLACES_BASE_URL}/{path}"
        try:
            response = await self._http.get(url, params={**params, "key": self._key})
        except httpx.HTTPError as exc:
            # The request URL carries the key; report the failure type only.
            raise MapsError(f"could not reach Google Maps ({type(exc).__name__})") from None
        if response.status_code >= 400:
            raise MapsError(f"Google Maps returned HTTP {response.status_code}")
        body = response.json()
        status = body.get("status")
        if status not in ok:
            raise MapsError(f"Google Maps error {status}: {body.get('error_message', '')}"[:300])
        return body

    async def text_search(self, query: str) -> list[Place]:
        body = await self._get("textsearch/json", {"query": query})
        places = [_place(result) for result in body.get("results") or []]
        return [place for place in places if place is not None][:MAX_RESULTS]

    async def place_details(self, place_id: str) -> PlaceDetails:
        body = await self._get("details/json", {"place_id": place_id, "fields": DETAILS_FIELDS})
        result = body.get("result")
        if not isinstance(result, dict) or not result:
            raise MapsError("Google Maps has no details for that place")
        return _details(place_id, result)

    async def directions(self, origin: str, destination: str, mode: str) -> Route | None:
        if mode not in TRAVEL_MODES:
            raise MapsError(f"unsupported travel mode {mode!r}")
        params = {"origin": origin, "destination": destination, "mode": mode}
        if mode in ("driving", "transit"):
            params["departure_time"] = "now"  # live traffic / next departures
        # NOT_FOUND: an end couldn't be geocoded. Like ZERO_RESULTS, that's "no route".
        body = await self._get(DIRECTIONS_URL, params, ok=("OK", "ZERO_RESULTS", "NOT_FOUND"))
        return _route(mode, body) if body.get("status") == "OK" else None


@dataclass
class MockMaps:
    """Returns the seeded places for every search and records the queries."""

    places: list[Place] = field(default_factory=list)
    details: dict[str, PlaceDetails] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)  # keyed by mode
    queries: list[str] = field(default_factory=list)
    detail_requests: list[str] = field(default_factory=list)
    route_requests: list[tuple[str, str, str]] = field(default_factory=list)

    async def text_search(self, query: str) -> list[Place]:
        self.queries.append(query)
        return self.places[:MAX_RESULTS]

    async def place_details(self, place_id: str) -> PlaceDetails:
        self.detail_requests.append(place_id)
        if place_id not in self.details:
            raise MapsError("no details seeded for that place")
        return self.details[place_id]

    async def directions(self, origin: str, destination: str, mode: str) -> Route | None:
        self.route_requests.append((origin, destination, mode))
        return self.routes.get(mode)


def build_maps(config: MapsConfig | None = None) -> Maps:
    config = config or MapsConfig.from_env()
    if config.has_credentials:
        return GoogleMaps(config)
    logger.info("GOOGLE_MAPS_API_KEY not set; using MockMaps")
    return MockMaps()
