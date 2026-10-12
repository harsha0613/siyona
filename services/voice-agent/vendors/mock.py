"""In-memory vendors. The whole call loop runs offline with no keys.

They exist to exercise the plumbing (streaming, speculation, teardown, timing)
in CI and in the offline demo; they say nothing about conversation quality.
Each one records enough about how it was used for tests to assert on it.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from vendors.base import LlmEvent, SttEvent, TextDelta, UsageReport

MULAW_SILENCE = b"\xff"
BYTES_PER_MS = 8  # 8 kHz, one byte per sample


async def precise_sleep(seconds: float) -> None:
    """``asyncio.sleep`` that honours short delays on Windows (~16 ms timer granularity)."""
    if seconds <= 0:
        return
    end = time.perf_counter() + seconds
    remaining = seconds
    while remaining > 0.02:
        await asyncio.sleep(remaining - 0.02)
        remaining = end - time.perf_counter()
    while time.perf_counter() < end:
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# STT: a scripted callee
# ---------------------------------------------------------------------------


class ScriptedSpeechToText:
    """Plays the callee's side from a script.

    Each line is "spoken" word by word as interim results, then emitted as a
    final with a speech-end time. The stream ends after the last line, which
    the pipeline sees as the callee hanging up.
    """

    def __init__(
        self,
        lines: list[str],
        *,
        word_delay_s: float = 0.0,
        endpoint_delay_s: float = 0.0,
        hang_up_after_script: bool = True,
    ) -> None:
        self._lines = list(lines)
        self._word_delay = word_delay_s
        self._endpoint_delay = endpoint_delay_s
        self._hang_up = hang_up_after_script
        self.audio_received = 0
        self.started = False
        self.closed = False
        # (kind, text, perf_counter) for every event emitted, in order.
        self.emitted: list[tuple[str, str, float]] = []

    async def start(self) -> None:
        self.started = True

    async def send_audio(self, mulaw: bytes) -> None:
        self.audio_received += len(mulaw)

    async def events(self) -> AsyncIterator[SttEvent]:
        for line in self._lines:
            words = line.split()
            # Like a real recogniser, the whole line shows up as an interim result
            # first; the final only arrives once endpointing hears silence.
            for count in range(1, len(words) + 1):
                if self._word_delay:
                    await precise_sleep(self._word_delay)
                text = " ".join(words[:count])
                self.emitted.append(("interim", text, time.perf_counter()))
                yield SttEvent("interim", text)
            speech_end = time.perf_counter()
            if self._endpoint_delay:
                await precise_sleep(self._endpoint_delay)
            self.emitted.append(("final", line, time.perf_counter()))
            yield SttEvent("final", line, speech_ended_at=speech_end)
        if not self._hang_up:
            await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------


class MockLanguageModel:
    """Streams a canned reply token by token, like a chat-completions stream.

    Records every request (``requests``), the interim text it was shown
    (``seen_partials``), and how many streams are still open.
    """

    def __init__(
        self,
        token_delay_s: float = 0.0,
        first_token_delay_s: float = 0.0,
        reply: str | None = None,
    ) -> None:
        self._token_delay = token_delay_s
        self._first_token_delay = first_token_delay_s
        self._reply = reply
        self.requests: list[tuple[str, float]] = []
        self.seen_partials: list[str] = []
        self.finished_at: list[float] = []
        self.open_streams = 0
        self.closed = False

    @staticmethod
    def _last_user_text(messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content") or "")
        return ""

    @staticmethod
    def plan_reply(callee_says: str) -> str:
        lowered = callee_says.lower()
        if "press" in lowered or "menu" in lowered:
            return "Understood, I will wait for a person. Thank you."
        if "name" in lowered:
            return "Sure, the booking is under Asha. It is for four people at seven tonight."
        if "goodbye" in lowered or "bye" in lowered:
            return "Thank you for your help. Goodbye."
        return "Hello, I'm an AI assistant calling on behalf of a customer. Could you help me?"

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[LlmEvent]:
        text = self._last_user_text(messages)
        self.requests.append((text, time.perf_counter()))
        self.open_streams += 1
        try:
            reply = self._reply or self.plan_reply(text)
            if self._first_token_delay:
                await precise_sleep(self._first_token_delay)
            for index, token in enumerate(reply.split(" ")):
                if index and self._token_delay:
                    await precise_sleep(self._token_delay)
                yield TextDelta(token if index == 0 else " " + token)
            yield UsageReport(len(text.split()), len(reply.split()))
            self.finished_at.append(time.perf_counter())
        finally:
            self.open_streams -= 1

    def observe_partial(self, text: str) -> None:
        self.seen_partials.append(text)

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# TTS and audio out
# ---------------------------------------------------------------------------


class MockTextToSpeech:
    """Emits mu-law silence, ~60 ms of audio per word, as each text chunk arrives."""

    def __init__(self, first_audio_delay_s: float = 0.0) -> None:
        self._delay = first_audio_delay_s
        self.spoken: list[str] = []
        self.first_audio_at: list[float] = []
        self.open_streams = 0
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def synthesize(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        self.open_streams += 1
        try:
            first = True
            async for chunk in text:
                if first and self._delay:
                    await precise_sleep(self._delay)
                self.spoken.append(chunk)
                if first:
                    self.first_audio_at.append(time.perf_counter())
                first = False
                words = max(1, len(chunk.split()))
                yield MULAW_SILENCE * (60 * BYTES_PER_MS * words)
        finally:
            self.open_streams -= 1

    async def close(self) -> None:
        self.closed = True


class RecordingAudioOut:
    """Collects agent audio instead of playing it."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []

    @property
    def total_bytes(self) -> int:
        return sum(len(chunk) for chunk in self.chunks)

    async def send_audio(self, mulaw: bytes) -> None:
        self.chunks.append(mulaw)
