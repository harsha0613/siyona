"""Outbound WhatsApp client: mock by default, Graph API with credentials."""

from __future__ import annotations

import asyncio
import json

import httpx
from whatsapp import CloudApiClient, MockWhatsApp, WhatsAppConfig, build_whatsapp


def test_build_whatsapp_defaults_to_the_mock_without_a_token() -> None:
    assert isinstance(build_whatsapp(WhatsAppConfig()), MockWhatsApp)
    placeholder = WhatsAppConfig(access_token="your-whatsapp-access-token-here")
    assert isinstance(build_whatsapp(placeholder), MockWhatsApp)


def test_build_whatsapp_uses_the_cloud_api_with_a_token() -> None:
    assert isinstance(build_whatsapp(WhatsAppConfig(access_token="t")), CloudApiClient)


def test_cloud_api_send_text_payload() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={})

    client = CloudApiClient(
        WhatsAppConfig(access_token="t", phone_number_id="PNID"),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    asyncio.run(client.send_text("+15550100001", "hello"))

    assert seen["url"].endswith("/PNID/messages")
    assert seen["auth"] == "Bearer t"
    assert seen["body"]["to"] == "15550100001"
    assert seen["body"]["text"]["body"] == "hello"
