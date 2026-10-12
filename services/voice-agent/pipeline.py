"""Real-time call loop: streaming Deepgram STT -> GPT-4.1 -> streaming Cartesia TTS.

One :class:`CallPipeline` runs per call. It only talks to the protocols in
``vendors/base.py``; the live adapters and the in-memory mocks are
interchangeable, and :func:`vendors.factory.build_vendors` picks the mocks
whenever API keys are absent, so this runs end to end with no keys.

Streaming, stage by stage:

* **Partial transcripts reach the model early.** While the callee is still
  talking, each interim transcript (after a short debounce) starts a
  *speculative* model request. If the final transcript matches the last
  speculation, that already-running stream becomes the reply, so the model has
  been working since before the callee stopped. If it doesn't match, the
  speculation is cancelled and its vendor stream closed.
* **TTS streams.** Model tokens are cut at phrase boundaries and fed into TTS
  while the model is still generating, so audio starts before the sentence is
  complete.
* **Teardown is total.** However the call ends (hang-up, error, cancellation),
  every in-flight model stream, TTS stream, timer and vendor socket is closed.
* **No dead air on a vendor stall.** If no reply audio has gone out
  ``STALL_FILLER_MS`` after the final transcript, a short filler ("One moment,
  please.") is played from audio synthesised at call start, so it works even
  when TTS itself is the stalled vendor.

Latency is measured per turn as three contiguous intervals, so they sum to the
silence the callee experiences (see ``latency.py``):

    stt_ms    end of callee speech   -> final transcript   (endpointing)
    model_ms  final transcript       -> first phrase handed to TTS
    tts_ms    first phrase to TTS    -> first agent audio sent to the line

Run it directly for an offline demo conversation:

    python services/voice-agent/pipeline.py
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from latency import (
    TARGET_TURN_LATENCY_MS,
    StageTimings,
    TurnStopwatch,
    log_call_summary,
    log_turn,
    slow_turn_ms_from_env,
)
from vendors.base import (
    AudioOut,
    LanguageModel,
    LlmEvent,
    SpeechToText,
    TextDelta,
    TextToSpeech,
    ToolCallRequest,
    UsageReport,
)
from vendors.mock import (
    MockLanguageModel,
    MockTextToSpeech,
    RecordingAudioOut,
    ScriptedSpeechToText,
)

logger = logging.getLogger("siyona.voice_agent.pipeline")

__all__ = ["TARGET_TURN_LATENCY_MS", "CallPipeline", "StageTimings", "TurnResult"]

# Interim transcripts shorter than this aren't worth a speculative request.
MIN_SPECULATION_WORDS = 2
# Wait this long for the interim transcript to settle before speculating.
SPECULATION_DEBOUNCE_S = 0.15
# Play a filler if no reply audio has gone out this long after the final transcript.
DEFAULT_STALL_FILLER_MS = 800.0
FILLER_TEXT = "One moment, please."

SYSTEM_PROMPT = (
    "You are Siyona, an AI assistant on a live phone call on behalf of a customer. "
    "Say that you are an AI assistant calling on someone's behalf. Speak naturally and "
    "keep every reply to one or two short sentences."
)


def stall_filler_s_from_env() -> float:
    return float(os.environ.get("STALL_FILLER_MS", DEFAULT_STALL_FILLER_MS)) / 1000.0


@dataclass
class TurnResult:
    caller_text: str
    agent_text: str
    audio_bytes: int
    timings: StageTimings
    speculative_hit: bool = False
    filler_played: bool = False


class SentenceChunker:
    """Groups streamed tokens into speakable phrases.

    The first phrase is released at the earliest clause boundary so audio starts
    quickly; later ones wait for a longer phrase so prosody stays natural.
    """

    BOUNDARY = re.compile(r"[.!?;:,](?=\s)|\n")

    def __init__(self, first_min_chars: int = 12, min_chars: int = 40) -> None:
        self._buffer = ""
        self._first_min = first_min_chars
        self._min = min_chars
        self._emitted = False

    def feed(self, text: str) -> list[str]:
        self._buffer += text
        chunks: list[str] = []
        while True:
            threshold = self._min if self._emitted else self._first_min
            cut = None
            for match in self.BOUNDARY.finditer(self._buffer):
                if match.end() >= threshold:
                    cut = match.end()
                    break
            if cut is None:
                return chunks
            chunk, self._buffer = self._buffer[:cut], self._buffer[cut:]
            if chunk.strip():
                chunks.append(chunk)
                self._emitted = True

    def flush(self) -> str | None:
        chunk, self._buffer = self._buffer, ""
        return chunk if chunk.strip() else None


def normalise(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", text.lower()).split())


class _Generation:
    """One model request, drained into a queue so it can run ahead of its consumer."""

    def __init__(self, llm: LanguageModel, messages: list[dict[str, Any]], text: str) -> None:
        self.text = text
        self.key = normalise(text)
        self._queue: asyncio.Queue[LlmEvent | BaseException | None] = asyncio.Queue()
        self._task = asyncio.create_task(self._pump(llm, messages))

    async def _pump(self, llm: LanguageModel, messages: list[dict[str, Any]]) -> None:
        try:
            async with contextlib.aclosing(llm.stream(messages, [])) as events:  # type: ignore[type-var]
                async for event in events:
                    self._queue.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._queue.put_nowait(exc)
            return
        self._queue.put_nowait(None)

    async def events(self) -> AsyncIterator[LlmEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def cancel(self) -> None:
        if not self._task.done():
            self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task


@dataclass
class CallPipeline:
    """Wires STT -> LLM -> TTS into a single streaming conversation loop."""

    stt: SpeechToText
    llm: LanguageModel
    tts: TextToSpeech
    audio_out: AudioOut
    system_prompt: str = SYSTEM_PROMPT
    speculate: bool = True
    speculation_debounce_s: float = SPECULATION_DEBOUNCE_S
    slow_turn_ms: float = field(default_factory=slow_turn_ms_from_env)
    stall_filler_s: float = field(default_factory=stall_filler_s_from_env)
    filler_text: str = FILLER_TEXT
    clock: Callable[[], float] = time.perf_counter
    history: list[dict[str, Any]] = field(default_factory=list)
    turns: list[TurnResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._speculation: _Generation | None = None
        self._debounce: asyncio.Task[None] | None = None
        self._filler_audio: bytes | None = None
        self._filler_task: asyncio.Task[None] | None = None
        self._closed = False

    # ------------------------------------------------------------------ loop

    async def run(self) -> list[TurnResult]:
        try:
            await asyncio.gather(self.stt.start(), self.tts.start())
            self._filler_task = asyncio.create_task(self._prepare_filler())
            async with contextlib.aclosing(self.stt.events()) as events:  # type: ignore[type-var]
                async for event in events:
                    if event.kind == "interim":
                        self._on_interim(event.text)
                    elif event.kind == "final" and event.text.strip():
                        await self._respond(event.text.strip(), event.speech_ended_at)
        finally:
            await self.aclose()
        return self.turns

    def _messages(self, caller_text: str) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": self.system_prompt},
            *self.history,
            {"role": "user", "content": caller_text},
        ]

    # ----------------------------------------------------------- speculation

    def _on_interim(self, text: str) -> None:
        observe = getattr(self.llm, "observe_partial", None)
        if observe is not None:
            observe(text)
        if not self.speculate or len(text.split()) < MIN_SPECULATION_WORDS:
            return
        if self._debounce is not None and not self._debounce.done():
            self._debounce.cancel()
        self._debounce = asyncio.create_task(self._speculate_after(text))

    async def _speculate_after(self, text: str) -> None:
        if self.speculation_debounce_s:
            await asyncio.sleep(self.speculation_debounce_s)
        if self._speculation is not None and self._speculation.key == normalise(text):
            return
        await self._drop_speculation()
        self._speculation = _Generation(self.llm, self._messages(text), text)
        logger.debug("speculating on %d interim words", len(text.split()))

    async def _drop_speculation(self) -> None:
        speculation, self._speculation = self._speculation, None
        if speculation is not None:
            await speculation.cancel()

    async def _cancel_debounce(self) -> None:
        debounce, self._debounce = self._debounce, None
        if debounce is not None and not debounce.done():
            debounce.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await debounce

    async def _generation_for(self, caller_text: str) -> tuple[_Generation, bool]:
        await self._cancel_debounce()
        speculation, self._speculation = self._speculation, None
        if speculation is not None and speculation.key == normalise(caller_text):
            return speculation, True
        if speculation is not None:
            await speculation.cancel()
        return _Generation(self.llm, self._messages(caller_text), caller_text), False

    # ------------------------------------------------------------------ turn

    async def _respond(self, caller_text: str, speech_end: float | None) -> TurnResult:
        watch = TurnStopwatch(speech_end, self.clock)
        generation, hit = await self._generation_for(caller_text)

        text_queue: asyncio.Queue[str | None] = asyncio.Queue()
        speak_task = asyncio.create_task(self._speak(text_queue, watch))
        filler_played: list[bool] = []
        watchdog = asyncio.create_task(self._filler_if_stalled(watch, filler_played))
        chunker = SentenceChunker()
        parts: list[str] = []

        def hand_to_tts(chunk: str) -> None:
            watch.mark_first_chunk()
            text_queue.put_nowait(chunk)

        try:
            async for event in generation.events():
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                    for chunk in chunker.feed(event.text):
                        hand_to_tts(chunk)
                elif isinstance(event, ToolCallRequest):
                    logger.warning("model requested tool %s; tools are not enabled", event.name)
                elif isinstance(event, UsageReport):
                    pass
            tail = chunker.flush()
            if tail:
                hand_to_tts(tail)
            text_queue.put_nowait(None)
            audio_bytes = await speak_task
        finally:
            for task in (speak_task, watchdog):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            await generation.cancel()

        timings = watch.timings()
        agent_text = "".join(parts).strip()
        self.history.append({"role": "user", "content": caller_text})
        if agent_text:
            self.history.append({"role": "assistant", "content": agent_text})
        turn = TurnResult(
            caller_text,
            agent_text,
            audio_bytes,
            timings,
            speculative_hit=hit,
            filler_played=bool(filler_played),
        )
        self.turns.append(turn)
        log_turn(
            timings,
            turn_index=len(self.turns),
            slow_turn_ms=self.slow_turn_ms,
            filler_played=turn.filler_played,
            speculative_hit=hit,
        )
        return turn

    async def _speak(self, text_queue: asyncio.Queue[str | None], watch: TurnStopwatch) -> int:
        async def text_stream() -> AsyncIterator[str]:
            while True:
                chunk = await text_queue.get()
                if chunk is None:
                    return
                yield chunk

        sent = 0
        async with contextlib.aclosing(self.tts.synthesize(text_stream())) as stream:  # type: ignore[type-var]
            async for audio in stream:
                watch.mark_first_audio()
                sent += len(audio)
                await self.audio_out.send_audio(audio)
        return sent

    # ---------------------------------------------------------------- filler

    async def _synthesize(self, text: str) -> bytes:
        async def one() -> AsyncIterator[str]:
            yield text

        audio = b""
        async with contextlib.aclosing(self.tts.synthesize(one())) as stream:  # type: ignore[type-var]
            async for chunk in stream:
                audio += chunk
        return audio

    async def _prepare_filler(self) -> None:
        """Synthesise the filler once, off the critical path, so a TTS stall can't block it."""
        try:
            self._filler_audio = await self._synthesize(self.filler_text) or None
        except Exception:
            logger.warning("could not pre-synthesise the filler", exc_info=True)

    async def _filler_if_stalled(self, watch: TurnStopwatch, played: list[bool]) -> None:
        await asyncio.sleep(self.stall_filler_s)
        if watch.has_audio:
            return
        logger.warning(
            "no reply audio %.0fms after the caller finished; playing a filler",
            watch.elapsed_ms(),
        )
        audio = self._filler_audio
        if audio is None:
            # Not cached yet (or TTS failed at start): best effort, live.
            audio = await self._synthesize(self.filler_text)
        if audio and not watch.has_audio:
            await self.audio_out.send_audio(audio)
            played.append(True)

    # -------------------------------------------------------------- teardown

    async def aclose(self) -> None:
        """Release everything the call holds. Safe to call more than once."""
        if self._closed:
            return
        self._closed = True
        await self._cancel_debounce()
        await self._drop_speculation()
        filler, self._filler_task = self._filler_task, None
        if filler is not None and not filler.done():
            filler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await filler
        log_call_summary([turn.timings.total_ms for turn in self.turns])
        for vendor in (self.stt, self.tts, self.llm):
            close = getattr(vendor, "close", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:
                logger.exception("closing %s failed", type(vendor).__name__)


# Backwards-compatible name from the scaffold.
VoicePipeline = CallPipeline


@contextlib.asynccontextmanager
async def demo_pipeline(utterances: list[str]) -> AsyncIterator[CallPipeline]:
    pipeline = CallPipeline(
        stt=ScriptedSpeechToText(utterances, word_delay_s=0.01, endpoint_delay_s=0.02),
        llm=MockLanguageModel(token_delay_s=0.002),
        tts=MockTextToSpeech(),
        audio_out=RecordingAudioOut(),
        speculation_debounce_s=0.0,
    )
    try:
        yield pipeline
    finally:
        await pipeline.aclose()


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
