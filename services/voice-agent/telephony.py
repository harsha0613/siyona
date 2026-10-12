"""Telephony provider boundary.

Everything that places or ends a phone call goes through the :class:`Telephony`
protocol, so the vendor is swappable: the orchestrator never imports a vendor
SDK and never builds vendor-specific payloads.

Two adapters ship today:

- :class:`TwilioTelephony` — Twilio Programmable Voice. A call is placed with
  inline TwiML that connects the answered call to our media-stream websocket.
  The call's id and a short-lived signed token travel as stream parameters, and
  the voice service refuses any media stream whose token doesn't verify.
- :class:`MockTelephony` — in-memory, records what would have been dialled.
  It is the default whenever the ``TELEPHONY_*`` variables are absent, so local
  runs and CI need no vendor account.

Adding a vendor means writing one class with ``place_call`` / ``hang_up`` and
registering it in :func:`build_telephony`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable
from urllib.parse import urlencode
from xml.sax.saxutils import escape

import httpx

logger = logging.getLogger("siyona.voice.telephony")

SIGNATURE_HEADER = "X-Twilio-Signature"
STREAM_TOKEN_PURPOSE = "media-stream"


class TelephonyError(RuntimeError):
    pass


@runtime_checkable
class Telephony(Protocol):
    """What the rest of the system may ask of a telephony vendor."""

    async def place_call(self, *, to: str, call_id: str, stream_token: str) -> str:
        """Start dialling ``to`` (E.164); returns the provider's call id."""
        ...

    async def hang_up(self, provider_call_sid: str) -> None:
        """End the call. Hanging up a call that is already over is not an error."""
        ...


@dataclass(frozen=True)
class TelephonyConfig:
    account_sid: str = ""
    auth_token: str = ""
    from_number: str = ""
    voice_public_url: str = "http://localhost:8001"
    max_call_duration_s: int = 600

    @classmethod
    def from_env(cls) -> TelephonyConfig:
        return cls(
            account_sid=os.environ.get("TELEPHONY_ACCOUNT_SID", ""),
            auth_token=os.environ.get("TELEPHONY_AUTH_TOKEN", ""),
            from_number=os.environ.get("TELEPHONY_FROM_NUMBER", ""),
            voice_public_url=os.environ.get("VOICE_PUBLIC_URL", "http://localhost:8001"),
            max_call_duration_s=int(os.environ.get("MAX_CALL_DURATION_S", "600")),
        )

    @property
    def has_credentials(self) -> bool:
        placeholders = ("", "your-telephony-account-sid-here", "your-telephony-auth-token-here")
        return self.account_sid not in placeholders and self.auth_token not in placeholders


def media_stream_url(voice_public_url: str) -> str:
    base = voice_public_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base.removeprefix("https://")
    elif base.startswith("http://"):
        base = "ws://" + base.removeprefix("http://")
    return f"{base}/twilio/media"


def _attr(value: str) -> str:
    return '"' + escape(value, {'"': "&quot;"}) + '"'


def build_twiml(stream_url: str, call_id: str, stream_token: str) -> str:
    return (
        "<Response><Connect>"
        f"<Stream url={_attr(stream_url)}>"
        f'<Parameter name="call_id" value={_attr(call_id)}/>'
        f'<Parameter name="token" value={_attr(stream_token)}/>'
        "</Stream></Connect></Response>"
    )


def compute_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's request signature: HMAC-SHA1 over the URL plus sorted POST params."""
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def validate_signature(
    auth_token: str, url: str, params: dict[str, str], signature: str | None
) -> bool:
    if not signature or not auth_token:
        return False
    return hmac.compare_digest(compute_signature(auth_token, url, params), signature)


class TwilioTelephony:
    def __init__(self, config: TelephonyConfig, http: httpx.AsyncClient | None = None) -> None:
        self._config = config
        self._base = f"https://api.twilio.com/2010-04-01/Accounts/{config.account_sid}"
        self._auth = (config.account_sid, config.auth_token)
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))

    def _callback(self, path: str, call_id: str) -> str:
        base = self._config.voice_public_url.rstrip("/")
        return f"{base}{path}?{urlencode({'call_id': call_id})}"

    async def place_call(self, *, to: str, call_id: str, stream_token: str) -> str:
        data: dict[str, str | list[str]] = {
            "To": to,
            "From": self._config.from_number,
            "Twiml": build_twiml(
                media_stream_url(self._config.voice_public_url), call_id, stream_token
            ),
            "StatusCallback": self._callback("/twilio/status", call_id),
            "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            "Timeout": "30",
            "TimeLimit": str(self._config.max_call_duration_s),
        }
        try:
            response = await self._http.post(f"{self._base}/Calls.json", data=data, auth=self._auth)
        except httpx.HTTPError as exc:
            raise TelephonyError(f"could not reach Twilio: {exc}") from exc
        if response.status_code >= 400:
            raise TelephonyError(
                f"Twilio refused the call ({response.status_code}): {response.text[:300]}"
            )
        return str(response.json()["sid"])

    async def hang_up(self, provider_call_sid: str) -> None:
        try:
            response = await self._http.post(
                f"{self._base}/Calls/{provider_call_sid}.json",
                data={"Status": "completed"},
                auth=self._auth,
            )
        except httpx.HTTPError as exc:
            raise TelephonyError(f"hang-up failed: {exc}") from exc
        # 404 means the call is already gone, which is what we wanted.
        if response.status_code >= 400 and response.status_code != 404:
            raise TelephonyError(f"hang-up failed ({response.status_code})")


@dataclass
class MockTelephony:
    """Records calls instead of placing them. No network, no keys."""

    placed: list[tuple[str, str]] = field(default_factory=list)
    hung_up: list[str] = field(default_factory=list)

    async def place_call(self, *, to: str, call_id: str, stream_token: str) -> str:
        sid = f"MOCK{uuid.uuid4().hex[:28]}"
        self.placed.append((to, call_id))
        return sid

    async def hang_up(self, provider_call_sid: str) -> None:
        self.hung_up.append(provider_call_sid)


def build_telephony(config: TelephonyConfig | None = None) -> Telephony:
    """Pick the adapter: Twilio when credentials are configured, otherwise the mock."""
    config = config or TelephonyConfig.from_env()
    if config.has_credentials:
        return TwilioTelephony(config)
    logger.info("telephony credentials not set; using MockTelephony")
    return MockTelephony()
