"""Cartesia streaming TTS over one websocket per call.

Each reply is a Cartesia *context*: text chunks are sent with ``continue: true``
as the model produces them, so synthesis starts on the first phrase and prosody
carries across chunks. Audio comes back as raw 8 kHz mu-law, ready for the
phone line. Closing the returned iterator (barge-in) cancels the context so
Cartesia stops generating, and billing, immediately.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from websockets.asyncio.client import ClientConnection, connect

logger = logging.getLogger("siyona.voice.cartesia")

URL = "wss://api.cartesia.ai/tts/websocket"


class TtsError(RuntimeError):
    pass


class CartesiaTextToSpeech:
    def __init__(
        self,
        api_key: str,
        *,
        version: str,
        model_id: str,
        voice_id: str,
        language: str,
    ) -> None:
        self._api_key = api_key
        self._version = version
        self._model_id = model_id
        self._voice_id = voice_id
        self._language = language
        self._ws: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._contexts: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.closed = False

    async def start(self) -> None:
        self._ws = await connect(
            URL,
            additional_headers={
                "X-API-Key": self._api_key,
                "Cartesia-Version": self._version,
            },
            open_timeout=5,
            max_size=2**22,
        )
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                message = json.loads(raw)
                queue = self._contexts.get(message.get("context_id", ""))
                if queue is not None:
                    queue.put_nowait(message)
        except Exception:
            logger.exception("cartesia socket closed")
        finally:
            for queue in self._contexts.values():
                queue.put_nowait({"type": "error", "error": "socket closed"})

    def _request(self, context_id: str, transcript: str, more: bool) -> str:
        return json.dumps(
            {
                "model_id": self._model_id,
                "transcript": transcript,
                "voice": {"mode": "id", "id": self._voice_id},
                "language": self._language,
                "context_id": context_id,
                "output_format": {
                    "container": "raw",
                    "encoding": "pcm_mulaw",
                    "sample_rate": 8000,
                },
                "continue": more,
            }
        )

    async def _send_text(
        self, context_id: str, text: AsyncIterator[str], queue: asyncio.Queue[dict[str, Any]]
    ) -> None:
        assert self._ws is not None
        sent_any = False
        async for chunk in text:
            if chunk.strip():
                await self._ws.send(self._request(context_id, chunk, more=True))
                sent_any = True
        if sent_any:
            await self._ws.send(self._request(context_id, "", more=False))
        else:
            queue.put_nowait({"type": "done", "context_id": context_id})

    async def synthesize(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        if self._ws is None:
            raise TtsError("call start() first")
        context_id = uuid.uuid4().hex
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._contexts[context_id] = queue
        sender = asyncio.create_task(self._send_text(context_id, text, queue))
        finished = False
        try:
            while True:
                if sender.done():
                    if sender.exception() is not None:
                        raise TtsError("sending text failed") from sender.exception()
                    message = await queue.get()
                else:
                    # Wait for audio, but notice if sending text fails meanwhile.
                    getter = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        {getter, sender}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if getter not in done:
                        getter.cancel()
                        continue
                    message = getter.result()
                kind = message.get("type")
                if kind == "chunk" and message.get("data"):
                    yield base64.b64decode(message["data"])
                elif kind == "done" or message.get("done"):
                    finished = True
                    return
                elif kind == "error":
                    raise TtsError(str(message.get("error", "unknown error")))
        finally:
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender
            self._contexts.pop(context_id, None)
            if not finished and self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.send(json.dumps({"context_id": context_id, "cancel": True}))

    async def close(self) -> None:
        self.closed = True
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
