"""Streaming STT -> LLM -> TTS: early partials, streamed audio, clean teardown."""

from __future__ import annotations

import asyncio

import pytest
from pipeline import CallPipeline, SentenceChunker, normalise
from vendors.base import SttEvent
from vendors.mock import (
    MockLanguageModel,
    MockTextToSpeech,
    RecordingAudioOut,
    ScriptedSpeechToText,
)

LONG_REPLY = (
    "Hello, I'm an AI assistant calling for a customer. I'd like to book a table for four "
    "people at seven tonight, under the name Asha. Is that possible?"
)


def _pipeline(stt, llm=None, tts=None, **kwargs) -> CallPipeline:
    return CallPipeline(
        stt=stt,
        llm=llm or MockLanguageModel(),
        tts=tts or MockTextToSpeech(),
        audio_out=RecordingAudioOut(),
        **kwargs,
    )


def _leftover_tasks() -> list[asyncio.Task]:
    return [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]


# --- AC: partial transcripts reach the model before the caller finishes -------


def test_partial_transcripts_reach_the_model_before_the_final() -> None:
    stt = ScriptedSpeechToText(
        ["Hi there, I'd like to check a booking"], word_delay_s=0.02, endpoint_delay_s=0.05
    )
    llm = MockLanguageModel()
    pipeline = _pipeline(stt, llm, speculation_debounce_s=0.0)
    [turn] = asyncio.run(pipeline.run())

    [final_at] = [at for kind, _, at in stt.emitted if kind == "final"]
    early = [(text, at) for text, at in llm.requests if at < final_at]
    assert early, "the model saw nothing before the caller finished"
    # It was working on the caller's words while they were still talking...
    assert any(len(text.split()) < 7 for text, _ in early)
    # ...and the matching speculation became the reply: no new request after the final.
    assert all(at < final_at for _, at in llm.requests)
    assert turn.speculative_hit
    assert turn.agent_text


def test_mismatched_speculation_is_discarded_and_closed() -> None:
    class Revising(ScriptedSpeechToText):
        async def events(self):
            async for event in super().events():
                if event.kind == "final":
                    yield type(event)("final", "Actually, a different question", None)
                else:
                    yield event

    stt = Revising(["one two three"], word_delay_s=0.02)
    llm = MockLanguageModel(token_delay_s=0.05)
    [turn] = asyncio.run(_pipeline(stt, llm, speculation_debounce_s=0.0).run())

    assert not turn.speculative_hit
    assert llm.requests[-1][0] == "Actually, a different question"
    assert llm.open_streams == 0


def test_speculation_is_debounced() -> None:
    stt = ScriptedSpeechToText(["a b c d e f g h"], word_delay_s=0.0)
    llm = MockLanguageModel()
    asyncio.run(_pipeline(stt, llm, speculation_debounce_s=0.5).run())

    # Words arrived faster than the debounce, so only the final request was made.
    assert len(llm.requests) == 1


def test_speculation_can_be_disabled() -> None:
    stt = ScriptedSpeechToText(["one two three four"], word_delay_s=0.01)
    llm = MockLanguageModel()
    [turn] = asyncio.run(_pipeline(stt, llm, speculate=False).run())

    assert len(llm.requests) == 1
    assert not turn.speculative_hit
    assert llm.seen_partials  # the partials were still observed


# --- AC: TTS streams instead of waiting for the full utterance ----------------


def test_audio_starts_before_the_model_finishes() -> None:
    stt = ScriptedSpeechToText(["hello"])
    llm = MockLanguageModel(token_delay_s=0.01, reply=LONG_REPLY)
    tts = MockTextToSpeech()
    audio = RecordingAudioOut()
    pipeline = CallPipeline(stt=stt, llm=llm, tts=tts, audio_out=audio, speculate=False)
    [turn] = asyncio.run(pipeline.run())

    assert tts.first_audio_at[0] < llm.finished_at[0]
    assert len(tts.spoken) > 1, "reply was synthesised in one piece"
    assert "".join(tts.spoken).strip() == LONG_REPLY
    assert turn.audio_bytes == audio.total_bytes > 0


def test_chunker_releases_the_first_phrase_early() -> None:
    chunker = SentenceChunker()
    out: list[str] = []
    for token in "Hello, I'm an assistant. I'd like to book a table please.".split(" "):
        out += chunker.feed(token + " ")
    assert out[0] == "Hello, I'm an assistant."
    assert chunker.flush().strip() == "I'd like to book a table please."


def test_normalise_ignores_case_and_punctuation() -> None:
    assert normalise("Hello,  World!") == normalise("hello world")


def test_stage_timings_cover_the_turn() -> None:
    stt = ScriptedSpeechToText(["hello there"], endpoint_delay_s=0.03)
    llm = MockLanguageModel(first_token_delay_s=0.02)
    tts = MockTextToSpeech(first_audio_delay_s=0.02)
    [turn] = asyncio.run(_pipeline(stt, llm, tts, speculate=False).run())

    t = turn.timings
    assert t.stt_ms >= 25
    assert t.model_ms >= 15
    assert t.tts_ms >= 15
    assert t.total_ms == pytest.approx(t.stt_ms + t.model_ms + t.tts_ms)


# --- AC: no sockets/streams left open after teardown --------------------------


def _assert_all_closed(stt, llm, tts) -> None:
    assert stt.closed
    assert tts.closed
    assert llm.closed
    assert llm.open_streams == 0
    assert tts.open_streams == 0


def test_teardown_after_a_normal_call() -> None:
    stt = ScriptedSpeechToText(["hello there", "goodbye now"], word_delay_s=0.01)
    llm, tts = MockLanguageModel(), MockTextToSpeech()

    async def run() -> list:
        await _pipeline(stt, llm, tts, speculation_debounce_s=0.0).run()
        return _leftover_tasks()

    assert asyncio.run(run()) == []
    _assert_all_closed(stt, llm, tts)


def test_teardown_when_the_call_is_cancelled_mid_reply() -> None:
    stt = ScriptedSpeechToText(["hello there"], hang_up_after_script=False)
    llm = MockLanguageModel(token_delay_s=0.05, reply=LONG_REPLY)
    tts = MockTextToSpeech()

    async def run() -> list:
        task = asyncio.create_task(_pipeline(stt, llm, tts, speculate=False).run())
        while not tts.spoken:
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return _leftover_tasks()

    assert asyncio.run(run()) == []
    _assert_all_closed(stt, llm, tts)


class _HangsUpMidSentence(ScriptedSpeechToText):
    """Interim results only; the stream ends before any final (the callee hung up)."""

    async def events(self):
        for text in ("one two", "one two three"):
            yield SttEvent("interim", text)
            await asyncio.sleep(0.02)


def test_teardown_when_the_caller_hangs_up_while_speculating() -> None:
    stt = _HangsUpMidSentence([])
    llm, tts = MockLanguageModel(token_delay_s=0.05), MockTextToSpeech()

    async def run() -> list:
        await _pipeline(stt, llm, tts, speculation_debounce_s=0.0).run()
        return _leftover_tasks()

    assert asyncio.run(run()) == []
    assert llm.requests, "speculation should have started"
    _assert_all_closed(stt, llm, tts)


class _FailingModel(MockLanguageModel):
    async def stream(self, messages, tools):
        self.open_streams += 1
        try:
            raise RuntimeError("model down")
            yield  # makes this an async generator
        finally:
            self.open_streams -= 1


def test_teardown_when_a_vendor_fails() -> None:

    stt = ScriptedSpeechToText(["hello there"])
    llm, tts = _FailingModel(), MockTextToSpeech()

    async def run() -> list:
        with pytest.raises(RuntimeError, match="model down"):
            await _pipeline(stt, llm, tts, speculate=False).run()
        return _leftover_tasks()

    assert asyncio.run(run()) == []
    _assert_all_closed(stt, llm, tts)


def test_aclose_is_idempotent() -> None:
    stt, llm, tts = ScriptedSpeechToText([]), MockLanguageModel(), MockTextToSpeech()
    pipeline = _pipeline(stt, llm, tts)

    async def run() -> None:
        await pipeline.aclose()
        await pipeline.aclose()

    asyncio.run(run())
    _assert_all_closed(stt, llm, tts)
