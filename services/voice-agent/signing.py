"""Short, expiring, tamper-proof tokens.

A token is ``<purpose>.<subject>.<expiry>.<mac>`` in URL-safe base64, HMAC'd
with ``LINK_SIGNING_KEY``. Purpose is part of the MAC, so a token minted for one
use (e.g. a media stream) cannot be replayed for another.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


class InvalidToken(ValueError):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: str, message: str) -> str:
    return _b64(hmac.new(key.encode(), message.encode(), hashlib.sha256).digest()[:24])


def sign(key: str, purpose: str, subject: str, ttl_s: int, now: float | None = None) -> str:
    expires = int((now if now is not None else time.time()) + ttl_s)
    body = f"{purpose}.{_b64(subject.encode())}.{expires}"
    return f"{body}.{_mac(key, body)}"


def verify(key: str, purpose: str, token: str, now: float | None = None) -> str:
    """Return the subject, or raise :class:`InvalidToken`."""
    try:
        token_purpose, subject_b64, expires_text, mac = token.split(".")
        expires = int(expires_text)
        subject = _unb64(subject_b64).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidToken("malformed token") from exc
    body = f"{token_purpose}.{subject_b64}.{expires_text}"
    if not hmac.compare_digest(_mac(key, body), mac):
        raise InvalidToken("bad signature")
    if token_purpose != purpose:
        raise InvalidToken("wrong purpose")
    if expires < (now if now is not None else time.time()):
        raise InvalidToken("expired")
    return subject
