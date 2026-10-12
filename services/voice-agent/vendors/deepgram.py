"""Deepgram live transcription over a websocket.

Audio goes in as the raw 8 kHz mu-law frames Twilio sends. Deepgram returns
interim results, ``is_final`` segments and ``speech_final`` (its endpointer
decided the utterance is over). Segments are accumulated and emitted as one
``final`` event per utterance; ``UtteranceEnd`` flushes anything left if
endpointing never fired (e.g. background noise kept the line busy).

``speech_ended_at`` is derived from the end time of the last recognised word.
Twilio streams in real time, so audio time ``t`` corresponds to wall time
``first_audio_sent_at + t``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

from websockets.asyncio.client import ClientConnection, connect

from vendors.base import SttEvent

logger = logging.getLogger("siyona.voice.deepgram")

URL = "wss://api.deepgram.com/v1/listen"
KEEPALIVE_S = 5.0


class DeepgramSpeechToText:
    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        language: str,
        endpointing_ms: int = 250,
        utterance_end_ms: int = 1000,
    ) -> None:
        self._api_key = api_key
        self._params = {
            "model": model,
            "language": language,
            "encoding": "mulaw",
            "sample_rate": 8000,
            "channels": 1,
            "interim_results": "true",
            "endpointing": endpointing_ms,
            "utterance_end_ms": utterance_end_ms,
            "vad_events": "true",
            "smart_format": "true",
            "punctuate": "true",
        }
        self._ws: ClientConnection | None = None
        self._audio_started_at: float | None = None
        self._last_send = 0.0
        self._keepalive: asyncio.Task[None] | None = None
        self.closed = False

    async def start(self) -> None:
        self._ws = await connect(
            f"{URL}?{urlencode(self._params)}",
            additional_headers={"Authorization": f"Token {self._api_key}"},
            open_timeout=5,
            max_size=2**20,
        )
        self._keepalive = asyncio.create_task(self._keep_alive())

    async def _keep_alive(self) -> None:
        while self._ws is not None:
            await asyncio.sleep(KEEPALIVE_S)
            if time.perf_counter() - self._last_send > KEEPALIVE_S and self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.send(json.dumps({"type": "KeepAlive"}))

    async def send_audio(self, mulaw: bytes) -> None:
        if self._ws is None:
            return
        now = time.perf_counter()
        if self._audio_started_at is None:
            self._audio_started_at = now
        self._last_send = now
        await self._ws.send(mulaw)

    def _wall_time(self, audio_seconds: float | None) -> float | None:
        if audio_seconds is None or self._audio_started_at is None:
            return None
        return self._audio_started_at + audio_seconds

    @staticmethod
    def _last_word_end(result: dict[str, Any]) -> float | None:
        words = result["channel"]["alternatives"][0].get("words") or []
        if words:
            return float(words[-1]["end"])
        if "start" in result and "duration" in result:
            return float(result["start"]) + float(result["duration"])
        return None

    async def events(self) -> AsyncIterator[SttEvent]:
        assert self._ws is not None, "call start() first"
        segments: list[str] = []
        last_word_end: float | None = None
        async for raw in self._ws:
            if isinstance(raw, bytes):
                continue
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "Results":
                text = message["channel"]["alternatives"][0].get("transcript", "").strip()
                if message.get("is_final"):
                    if text:
                        segments.append(text)
                        last_word_end = self._last_word_end(message)
                    if message.get("speech_final") and segments:
                        yield SttEvent("final", " ".join(segments), self._wall_time(last_word_end))
                        segments = []
                    elif text:
                        yield SttEvent("interim", " ".join(segments))
                elif text:
                    yield SttEvent("interim", " ".join([*segments, text]))
            elif kind == "UtteranceEnd":
                if segments:
                    end = message.get("last_word_end", last_word_end)
                    yield SttEvent("final", " ".join(segments), self._wall_time(end))
                    segments = []
            elif kind == "SpeechStarted":
                yield SttEvent("speech_started")
            elif kind == "Error" or "err_code" in message:
                logger.error("deepgram error: %s", str(message)[:300])

    async def close(self) -> None:
        self.closed = True
        ws, self._ws = self._ws, None
        if self._keepalive is not None:
            self._keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive
            self._keepalive = None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps({"type": "CloseStream"}))
            with contextlib.suppress(Exception):
                await ws.close()
