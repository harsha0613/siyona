"""The dispatcher places calls only through the telephony interface."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import signing
from dispatcher import CallDispatcher
from telephony import STREAM_TOKEN_PURPOSE, MockTelephony


def test_dial_places_one_call_with_a_verifiable_stream_token() -> None:
    seen: dict = {}

    class Spy(MockTelephony):
        async def place_call(self, *, to: str, call_id: str, stream_token: str) -> str:
            seen["token"] = stream_token
            return await super().place_call(to=to, call_id=call_id, stream_token=stream_token)

    phone = Spy()
    task = SimpleNamespace(id="t1", numbers=["+14155550123"], call_id=None, provider_call_sid=None)
    asyncio.run(CallDispatcher(phone, "key").dial(task))

    assert phone.placed == [("+14155550123", task.call_id)]
    assert task.provider_call_sid.startswith("MOCK")
    assert signing.verify("key", STREAM_TOKEN_PURPOSE, seen["token"]) == task.call_id


def test_dial_without_a_number_refuses() -> None:
    task = SimpleNamespace(id="t1", numbers=[], call_id=None, provider_call_sid=None)
    with pytest.raises(ValueError):
        asyncio.run(CallDispatcher(MockTelephony(), "key").dial(task))
