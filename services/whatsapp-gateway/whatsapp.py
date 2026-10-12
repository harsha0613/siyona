"""Outbound WhatsApp Business Cloud API client.

Everything the assistant says on WhatsApp goes through a :class:`WhatsAppSender`.
:class:`CloudApiClient` talks to the Graph API; :class:`MockWhatsApp` records
messages in memory and is the default when ``WHATSAPP_ACCESS_TOKEN`` is unset,
so local runs and CI never send anything.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger("siyona.whatsapp")

MAX_TEXT_CHARS = 4096


class WhatsAppError(RuntimeError):
    pass


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class WhatsAppSender(Protocol):
    async def send_text(self, to: str, body: str) -> None: ...


def _wa_to(phone: str) -> str:
    return phone.lstrip("+")


@dataclass(frozen=True)
class WhatsAppConfig:
    access_token: str = ""
    phone_number_id: str = ""
    api_version: str = "v21.0"

    @classmethod
    def from_env(cls) -> WhatsAppConfig:
        return cls(
            access_token=os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
            phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
            api_version=os.environ.get("WHATSAPP_API_VERSION", "v21.0"),
        )

    @property
    def has_credentials(self) -> bool:
        return self.access_token not in ("", "your-whatsapp-access-token-here")


class CloudApiClient:
    """Thin async client over the Graph API."""

    def __init__(self, config: WhatsAppConfig, http: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._base = f"https://graph.facebook.com/{config.api_version}"
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))

    async def _post_message(self, payload: dict[str, Any]) -> None:
        url = f"{self._base}/{self._config.phone_number_id}/messages"
        body = {"messaging_product": "whatsapp", **payload}
        headers = {"Authorization": f"Bearer {self._config.access_token}"}
        try:
            response = await self._http.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise WhatsAppError(f"WhatsApp request failed: {exc}") from exc
        if response.status_code >= 400:
            raise WhatsAppError(f"WhatsApp API {response.status_code}: {response.text[:300]}")

    async def send_text(self, to: str, body: str) -> None:
        await self._post_message(
            {
                "to": _wa_to(to),
                "type": "text",
                "text": {"body": _truncate(body, MAX_TEXT_CHARS), "preview_url": False},
            }
        )


@dataclass
class OutboxMessage:
    to: str
    body: str


@dataclass
class MockWhatsApp:
    """Records every message instead of sending it. Used in local runs and tests."""

    outbox: list[OutboxMessage] = field(default_factory=list)

    async def send_text(self, to: str, body: str) -> None:
        self.outbox.append(OutboxMessage(to, body))
        logger.info("mock whatsapp text (%d chars)", len(body))

    def texts_to(self, phone: str) -> list[str]:
        return [message.body for message in self.outbox if message.to == phone]


def build_whatsapp(config: WhatsAppConfig | None = None) -> WhatsAppSender:
    config = config or WhatsAppConfig.from_env()
    if config.has_credentials:
        return CloudApiClient(config)
    logger.info("WHATSAPP_ACCESS_TOKEN not set; using MockWhatsApp")
    return MockWhatsApp()
