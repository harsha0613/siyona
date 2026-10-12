"""Hand a confirmed task to the telephony provider.

The dispatcher is the only caller of :class:`telephony.Telephony`. It mints the
call id and the signed media-stream token, places the call, and records the
provider's call id on the task. It never sees vendor payloads.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import signing
from telephony import STREAM_TOKEN_PURPOSE, Telephony, build_telephony

logger = logging.getLogger("siyona.orchestrator.dispatcher")

DEV_LINK_SIGNING_KEY = "dev-link-key-not-for-production"
# The stream must connect within this window after dialling.
STREAM_TOKEN_TTL_S = 900


class CallDispatcher:
    def __init__(self, telephony: Telephony, signing_key: str) -> None:
        self.telephony = telephony
        self._signing_key = signing_key

    async def dial(self, task: Any) -> None:
        if not task.numbers:
            raise ValueError("task has no number to call")
        call_id = str(uuid.uuid4())
        token = signing.sign(self._signing_key, STREAM_TOKEN_PURPOSE, call_id, STREAM_TOKEN_TTL_S)
        sid = await self.telephony.place_call(
            to=task.numbers[0], call_id=call_id, stream_token=token
        )
        task.call_id, task.provider_call_sid = call_id, sid
        logger.info("call %s placed for task %s", call_id, task.id)


def signing_key_from_env() -> str:
    return os.environ.get("LINK_SIGNING_KEY", DEV_LINK_SIGNING_KEY)


def build_dispatcher() -> CallDispatcher:
    return CallDispatcher(build_telephony(), signing_key_from_env())
