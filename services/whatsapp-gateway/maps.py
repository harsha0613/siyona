"""Google Maps Platform boundary.

The assistant only ever talks to the :class:`Maps` protocol, so the vendor is
swappable and tests never touch the network:

- :class:`GoogleMaps` — the Places Text Search web service over httpx.
- :class:`MockMaps` — in-memory, returns only the places it was given. It is the
  default whenever ``GOOGLE_MAPS_API_KEY`` is unset or a placeholder, so local
  runs and CI need no key. With no places seeded it finds nothing: a keyless run
  never shows a business that doesn't exist.

The API key travels as a query parameter, so it is never logged and never put
into an error message.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger("siyona.gateway.maps")

PLACES_BASE_URL = "https://maps.googleapis.com/maps/api/place"
MAX_RESULTS = 5


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


class Maps(Protocol):
    async def text_search(self, query: str) -> list[Place]:
        """Places matching ``query`` (e.g. "pharmacy near 1 Main St, Worcester MA")."""
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


class GoogleMaps:
    def __init__(self, config: MapsConfig, http: httpx.AsyncClient | None = None) -> None:
        self._key = config.api_key
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))

    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._http.get(
                f"{PLACES_BASE_URL}/{path}", params={**params, "key": self._key}
            )
        except httpx.HTTPError as exc:
            # The request URL carries the key; report the failure type only.
            raise MapsError(f"could not reach Google Maps ({type(exc).__name__})") from None
        if response.status_code >= 400:
            raise MapsError(f"Google Maps returned HTTP {response.status_code}")
        body = response.json()
        status = body.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            raise MapsError(f"Google Maps error {status}: {body.get('error_message', '')}"[:300])
        return body

    async def text_search(self, query: str) -> list[Place]:
        body = await self._get("textsearch/json", {"query": query})
        places = [_place(result) for result in body.get("results") or []]
        return [place for place in places if place is not None][:MAX_RESULTS]


@dataclass
class MockMaps:
    """Returns the seeded places for every search and records the queries."""

    places: list[Place] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    async def text_search(self, query: str) -> list[Place]:
        self.queries.append(query)
        return self.places[:MAX_RESULTS]


def build_maps(config: MapsConfig | None = None) -> Maps:
    config = config or MapsConfig.from_env()
    if config.has_credentials:
        return GoogleMaps(config)
    logger.info("GOOGLE_MAPS_API_KEY not set; using MockMaps")
    return MockMaps()
