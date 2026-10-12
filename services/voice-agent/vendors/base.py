"""Interfaces between the call loop and its vendors.

The pipeline only ever talks to these protocols, so the live Deepgram / OpenAI
/ Cartesia implementations and the in-memory mocks are interchangeable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class SttEvent:
    """Something the speech recogniser noticed.

    ``final`` means the speaker has finished an utterance (endpointing fired),
    not merely that a segment is stable. ``speech_ended_at`` is the
    ``time.perf_counter()`` estimate of when their audio actually stopped, so the
    endpointing delay can be measured rather than assumed.
    """

    kind: Literal["interim", "final", "speech_started"]
    text: str = ""
    speech_ended_at: float | None = None


class SpeechToText(Protocol):
    async def start(self) -> None: ...

    async def send_audio(self, mulaw: bytes) -> None: ...

    def events(self) -> AsyncIterator[SttEvent]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: str  # raw JSON from the model; validated before dispatch


@dataclass(frozen=True)
class UsageReport:
    input_tokens: int
    output_tokens: int


LlmEvent = TextDelta | ToolCallRequest | UsageReport


class LanguageModel(Protocol):
    def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[LlmEvent]:
        """Stream the reply. Closing the iterator must release the vendor stream."""
        ...


class TextToSpeech(Protocol):
    async def start(self) -> None: ...

    def synthesize(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        """Stream 8 kHz mu-law audio for text that is itself still streaming in.

        Consumers cancel by closing the returned iterator; implementations must
        stop synthesis (and billing) promptly when that happens.
        """
        ...

    async def close(self) -> None: ...


class AudioOut(Protocol):
    """Where agent audio goes: the phone call, or a recorder in tests."""

    async def send_audio(self, mulaw: bytes) -> None: ...
