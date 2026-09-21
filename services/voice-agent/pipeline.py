"""Real-time voice pipeline: Deepgram STT -> GPT-4.1 -> Cartesia TTS.

This module is the streaming skeleton for the Siyona voice agent. The three
vendor clients sit behind small interfaces and the implementations used here
are in-memory mocks, so the loop runs end to end with no API keys.

Run it directly for a demo conversation:

    python services/voice-agent/pipeline.py
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

logger = logging.getLogger("siyona.voice_agent.pipeline")

# Budget for one full turn (caller stops speaking -> first audio byte out).
TARGET_TURN_LATENCY_MS = 300.0


@dataclass
class StageTimings:
    """Per-stage stopwatch for a single conversational turn."""

    stt_ms: float = 0.0
    model_ms: float = 0.0
    tts_ms: float = 0.0

    @property
    def total_ms(self) -> float:
        return self.stt_ms + self.model_ms + self.tts_ms

    @property
    def within_budget(self) -> bool:
        return self.total_ms <= TARGET_TURN_LATENCY_MS

    def as_dict(self) -> dict[str, float]:
        return {
            "stt_ms": round(self.stt_ms, 1),
            "model_ms": round(self.model_ms, 1),
            "tts_ms": round(self.tts_ms, 1),
            "total_ms": round(self.total_ms, 1),
        }


class _Stopwatch:
    """Context manager accumulating wall time into one StageTimings field."""

    def __init__(self, timings: StageTimings, field_name: str) -> None:
        self._timings = timings
        self._field = field_name
        self._start = 0.0

    def __enter__(self) -> _Stopwatch:
        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc_info: object) -> None:
        elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        setattr(self._timings, self._field, getattr(self._timings, self._field) + elapsed_ms)


@dataclass
class Transcript:
    """A partial or final transcript emitted by the STT socket."""

    text: str
    is_final: bool


# ---------------------------------------------------------------------------
# Mock vendor clients
# ---------------------------------------------------------------------------


class MockDeepgramSocket:
    """Stand-in for a Deepgram streaming websocket.

    Emits interim transcripts followed by a final one, mimicking the partial
    results that let the model start planning before the caller stops talking.
    """

    def __init__(self, utterances: list[str], chunk_delay_s: float = 0.005) -> None:
        self._utterances = list(utterances)
        self._chunk_delay_s = chunk_delay_s
        self.closed = False

    async def stream(self) -> AsyncIterator[Transcript]:
        for utterance in self._utterances:
            words = utterance.split()
            for idx in range(1, len(words)):
                await asyncio.sleep(self._chunk_delay_s)
                yield Transcript(text=" ".join(words[:idx]), is_final=False)
            await asyncio.sleep(self._chunk_delay_s)
            yield Transcript(text=utterance, is_final=True)

    async def close(self) -> None:
        self.closed = True


class MockLanguageModel:
    """Stand-in for GPT-4.1 with tool calling, streaming tokens back."""

    def __init__(self, token_delay_s: float = 0.002) -> None:
        self._token_delay_s = token_delay_s
        self.seen_partials: list[str] = []

    def observe_partial(self, transcript: Transcript) -> None:
        """Warm the model with interim transcripts (speculative planning)."""
        self.seen_partials.append(transcript.text)

    async def stream_reply(
        self, transcript: str, history: list[dict[str, str]]
    ) -> AsyncIterator[str]:
        reply = self._plan_reply(transcript, history)
        for token in reply.split():
            await asyncio.sleep(self._token_delay_s)
            yield token + " "

    @staticmethod
    def _plan_reply(transcript: str, history: list[dict[str, str]]) -> str:
        lowered = transcript.lower()
        if "press" in lowered or "menu" in lowered:
            return "Understood, selecting that option now."
        if "name" in lowered:
            return "I am calling on behalf of a customer to confirm their booking."
        if "goodbye" in lowered or "bye" in lowered:
            return "Thank you for your help. Goodbye."
        return f"Got it. Continuing on turn {len(history) // 2 + 1}."


class MockCartesiaTTS:
    """Stand-in for Cartesia streaming TTS; yields audio frames as they render."""

    def __init__(self, frame_delay_s: float = 0.002) -> None:
        self._frame_delay_s = frame_delay_s

    async def stream_audio(self, tokens: AsyncIterator[str]) -> AsyncIterator[bytes]:
        async for token in tokens:
            await asyncio.sleep(self._frame_delay_s)
            yield token.encode("utf-8")


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass
class TurnResult:
    caller_text: str
    agent_text: str
    audio_bytes: int
    timings: StageTimings


@dataclass
class VoicePipeline:
    """Wires STT -> LLM -> TTS into a single streaming conversation loop."""

    stt: MockDeepgramSocket
    llm: MockLanguageModel
    tts: MockCartesiaTTS
    history: list[dict[str, str]] = field(default_factory=list)
    turns: list[TurnResult] = field(default_factory=list)

    async def run(self) -> list[TurnResult]:
        async for turn in self.stream_turns():
            self.turns.append(turn)
        return self.turns

    async def stream_turns(self) -> AsyncIterator[TurnResult]:
        timings = StageTimings()
        stt_watch = _Stopwatch(timings, "stt_ms")
        stt_watch.__enter__()

        async for transcript in self.stt.stream():
            if not transcript.is_final:
                self.llm.observe_partial(transcript)
                continue

            stt_watch.__exit__()
            yield await self._handle_final(transcript.text, timings)

            timings = StageTimings()
            stt_watch = _Stopwatch(timings, "stt_ms")
            stt_watch.__enter__()

        stt_watch.__exit__()
        await self.stt.close()

    async def _handle_final(self, caller_text: str, timings: StageTimings) -> TurnResult:
        self.history.append({"role": "caller", "content": caller_text})

        tokens: list[str] = []
        audio_chunks: list[bytes] = []

        with _Stopwatch(timings, "model_ms"):
            async for token in self.llm.stream_reply(caller_text, self.history):
                tokens.append(token)

        with _Stopwatch(timings, "tts_ms"):
            async for chunk in self.tts.stream_audio(_aiter(tokens)):
                audio_chunks.append(chunk)

        agent_text = "".join(tokens).strip()
        self.history.append({"role": "agent", "content": agent_text})

        self._log_turn(caller_text, agent_text, timings)
        return TurnResult(
            caller_text=caller_text,
            agent_text=agent_text,
            audio_bytes=sum(len(chunk) for chunk in audio_chunks),
            timings=timings,
        )

    @staticmethod
    def _log_turn(caller_text: str, agent_text: str, timings: StageTimings) -> None:
        breakdown = timings.as_dict()
        logger.info(
            "turn complete caller=%r agent=%r stt=%.1fms model=%.1fms "
            "tts=%.1fms total=%.1fms budget=%s",
            caller_text,
            agent_text,
            breakdown["stt_ms"],
            breakdown["model_ms"],
            breakdown["tts_ms"],
            breakdown["total_ms"],
            "ok" if timings.within_budget else "EXCEEDED",
        )


async def _aiter(items: list[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


@asynccontextmanager
async def demo_pipeline(utterances: list[str]) -> AsyncIterator[VoicePipeline]:
    pipeline = VoicePipeline(
        stt=MockDeepgramSocket(utterances),
        llm=MockLanguageModel(),
        tts=MockCartesiaTTS(),
    )
    try:
        yield pipeline
    finally:
        await pipeline.stt.close()


DEMO_UTTERANCES = [
    "Thank you for calling, for billing press one",
    "May I have your name please",
    "That is all sorted, goodbye",
]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    async with demo_pipeline(DEMO_UTTERANCES) as pipeline:
        turns = await pipeline.run()

    print("\n--- latency summary ---")
    for idx, turn in enumerate(turns, start=1):
        print(f"turn {idx}: {turn.timings.as_dict()} audio={turn.audio_bytes}B")
    median = sorted(turn.timings.total_ms for turn in turns)[len(turns) // 2]
    print(f"median turn latency: {median:.1f} ms (target <= {TARGET_TURN_LATENCY_MS:.0f} ms)")


if __name__ == "__main__":
    asyncio.run(main())
