"""The telephony provider boundary: both adapters honour one interface."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest
import signing
from telephony import (
    MockTelephony,
    Telephony,
    TelephonyConfig,
    TelephonyError,
    TwilioTelephony,
    build_telephony,
    build_twiml,
    compute_signature,
    media_stream_url,
    validate_signature,
)

CALLEE = "+15550100001"
CONFIG = TelephonyConfig(
    account_sid="AC-test",
    auth_token="test-auth-token",
    from_number="+15550100099",
    voice_public_url="https://voice.example.test",
)


def _twilio(handler) -> TwilioTelephony:
    return TwilioTelephony(CONFIG, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("adapter", [MockTelephony(), _twilio(lambda r: httpx.Response(200))])
def test_every_adapter_satisfies_the_protocol(adapter) -> None:
    assert isinstance(adapter, Telephony)


def test_build_telephony_defaults_to_the_mock_without_credentials() -> None:
    assert isinstance(build_telephony(TelephonyConfig()), MockTelephony)
    placeholder = TelephonyConfig(
        account_sid="your-telephony-account-sid-here",
        auth_token="your-telephony-auth-token-here",
    )
    assert isinstance(build_telephony(placeholder), MockTelephony)


def test_build_telephony_picks_twilio_with_credentials() -> None:
    assert isinstance(build_telephony(CONFIG), TwilioTelephony)


def test_mock_records_calls_and_hang_ups() -> None:
    mock = MockTelephony()

    async def run() -> str:
        sid = await mock.place_call(to=CALLEE, call_id="call-1", stream_token="tok")
        await mock.hang_up(sid)
        return sid

    sid = asyncio.run(run())
    assert mock.placed == [(CALLEE, "call-1")]
    assert mock.hung_up == [sid]


def test_twilio_place_call_sends_twiml_and_returns_sid() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(201, json={"sid": "CA123"})

    sid = asyncio.run(_twilio(handler).place_call(to=CALLEE, call_id="c1", stream_token="tok"))
    assert sid == "CA123"
    assert seen["url"].endswith("/Accounts/AC-test/Calls.json")
    assert seen["form"]["To"] == [CALLEE]
    assert "wss://voice.example.test/twilio/media" in seen["form"]["Twiml"][0]


def test_twilio_refusal_raises_telephony_error() -> None:
    adapter = _twilio(lambda r: httpx.Response(400, text="bad number"))
    with pytest.raises(TelephonyError):
        asyncio.run(adapter.place_call(to=CALLEE, call_id="c1", stream_token="tok"))


def test_twilio_hang_up_tolerates_an_already_ended_call() -> None:
    asyncio.run(_twilio(lambda r: httpx.Response(404)).hang_up("CA123"))


def test_twiml_escapes_attribute_values() -> None:
    twiml = build_twiml("wss://x/media", 'id"<', "t&k")
    assert 'value="id&quot;&lt;"' in twiml
    assert 'value="t&amp;k"' in twiml


def test_media_stream_url_upgrades_the_scheme() -> None:
    assert media_stream_url("https://a.test/") == "wss://a.test/twilio/media"
    assert media_stream_url("http://a.test") == "ws://a.test/twilio/media"


def test_twilio_request_signature_round_trip() -> None:
    params = {"CallSid": "CA1", "CallStatus": "completed"}
    sig = compute_signature("token", "https://v.test/twilio/status", params)
    assert validate_signature("token", "https://v.test/twilio/status", params, sig)
    assert not validate_signature("other", "https://v.test/twilio/status", params, sig)
    assert not validate_signature("token", "https://v.test/twilio/status", params, None)


def test_stream_token_is_bound_to_purpose_and_expiry() -> None:
    token = signing.sign("key", "media-stream", "call-1", ttl_s=60, now=1000)
    assert signing.verify("key", "media-stream", token, now=1030) == "call-1"
    with pytest.raises(signing.InvalidToken):
        signing.verify("key", "transcript", token, now=1030)
    with pytest.raises(signing.InvalidToken):
        signing.verify("key", "media-stream", token, now=2000)
    with pytest.raises(signing.InvalidToken):
        signing.verify("other-key", "media-stream", token, now=1030)
