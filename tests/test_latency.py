"""Per-turn latency: stage breakdown logged, slow turns flagged, filler on a vendor stall."""

from __future__ import annotations

import asyncio
import logging

import pytest
from latency import StageTimings, TurnStopwatch, log_call_summary, log_turn, percentile
from pipeline import CallPipeline
from vendors.mock import (
    MockLanguageModel,
    MockTextToSpeech,
    RecordingAudioOut,
    ScriptedSpeechToText,
)

LATENCY_LOGGER = "siyona.voice_agent.latency"
PIPELINE_LOGGER = "siyona.voice_agent.pipeline"


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance_ms(self, ms: float) -> None:
        self.now += ms / 1000.0


def _latency_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LATENCY_LOGGER and hasattr(r, "latency")]


# --- the stopwatch --------------------------------------------------------------


def test_stopwatch_splits_the_turn_into_contiguous_stages() -> None:
    clock = FakeClock()
    speech_end = clock.now
    clock.advance_ms(80)  # endpointing
    watch = TurnStopwatch(speech_end, clock)
    clock.advance_ms(120)
    watch.mark_first_chunk()
    clock.advance_ms(5)
    watch.mark_first_chunk()  # later marks don't move it
    clock.advance_ms(75)
    watch.mark_first_audio()

    timings = watch.timings()
    assert timings.as_dict() == {
        "stt_ms": 80.0,
        "model_ms": 120.0,
        "tts_ms": 80.0,
        "total_ms": 280.0,
    }
    assert timings.within_budget
    assert timings.slowest_stage == "model"


def test_stopwatch_without_audio_reports_what_it_measured() -> None:
    clock = FakeClock()
    watch = TurnStopwatch(None, clock)
    clock.advance_ms(50)
    assert watch.timings().total_ms == 0.0
    assert not watch.has_audio
    assert watch.elapsed_ms() == pytest.approx(50.0)


# --- logging ----------------------------------------------------------------------


def test_every_turn_is_logged_with_its_stage_breakdown(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=LATENCY_LOGGER):
        slow = log_turn(StageTimings(80, 120, 80), turn_index=1, slow_turn_ms=500)

    assert slow is False
    [record] = _latency_records(caplog)
    assert record.levelno == logging.INFO
    assert record.latency["stt_ms"] == 80.0
    assert record.latency["model_ms"] == 120.0
    assert record.latency["tts_ms"] == 80.0
    assert "stt=80.0ms model=120.0ms tts=80.0ms" in record.getMessage()


def test_turns_over_500ms_are_flagged_with_the_breakdown(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=LATENCY_LOGGER):
        slow = log_turn(StageTimings(90, 450, 70), turn_index=3, slow_turn_ms=500)

    assert slow is True
    [record] = _latency_records(caplog)
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert "slow turn 3" in message
    assert "stt=90.0ms model=450.0ms tts=70.0ms" in message
    assert "slowest=model" in message


def test_exactly_500ms_is_not_slow() -> None:
    assert log_turn(StageTimings(100, 300, 100), turn_index=1, slow_turn_ms=500) is False


def test_percentiles_and_call_summary(caplog: pytest.LogCaptureFixture) -> None:
    values = [100.0, 200.0, 300.0, 400.0, 1000.0]
    assert percentile(values, 50) == 300.0
    assert percentile(values, 95) == 1000.0
    assert percentile([], 50) is None
    with caplog.at_level(logging.INFO, logger=LATENCY_LOGGER):
        log_call_summary(values)
    assert "median=300.0ms p95=1000.0ms" in caplog.text


# --- in the pipeline --------------------------------------------------------------


def _pipeline(llm=None, tts=None, lines=("hello there",), **kwargs) -> CallPipeline:
    return CallPipeline(
        stt=ScriptedSpeechToText(list(lines), word_delay_s=0.02),
        llm=llm or MockLanguageModel(),
        tts=tts or MockTextToSpeech(),
        audio_out=RecordingAudioOut(),
        speculate=False,
        **kwargs,
    )


def test_pipeline_logs_a_breakdown_for_every_turn(caplog: pytest.LogCaptureFixture) -> None:
    pipeline = _pipeline(lines=("hello there", "goodbye now"), slow_turn_ms=10_000)
    with caplog.at_level(logging.INFO, logger=LATENCY_LOGGER):
        turns = asyncio.run(pipeline.run())

    records = _latency_records(caplog)
    assert [r.latency["turn"] for r in records] == [1, 2]
    for record, turn in zip(records, turns, strict=True):
        assert record.latency["total_ms"] == round(turn.timings.total_ms, 1)
        assert {"stt_ms", "model_ms", "tts_ms"} <= set(record.latency)
    assert "call latency over 2 turns" in caplog.text


def test_pipeline_flags_a_slow_turn(caplog: pytest.LogCaptureFixture) -> None:
    llm = MockLanguageModel(first_token_delay_s=0.3)
    pipeline = _pipeline(llm, slow_turn_ms=200, stall_filler_s=5.0)
    with caplog.at_level(logging.INFO, logger=LATENCY_LOGGER):
        [turn] = asyncio.run(pipeline.run())

    assert turn.timings.total_ms > 200
    [record] = _latency_records(caplog)
    assert record.levelno == logging.WARNING
    assert "slowest=model" in record.getMessage()


def test_model_stall_plays_a_filler_instead_of_silence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = MockLanguageModel(first_token_delay_s=0.4)
    audio = RecordingAudioOut()
    pipeline = CallPipeline(
        stt=ScriptedSpeechToText(["hello there"], word_delay_s=0.02),
        llm=llm,
        tts=MockTextToSpeech(),
        audio_out=audio,
        speculate=False,
        stall_filler_s=0.1,
    )
    with caplog.at_level(logging.WARNING, logger=PIPELINE_LOGGER):
        [turn] = asyncio.run(pipeline.run())

    assert turn.filler_played
    filler = pipeline._filler_audio
    assert filler
    assert audio.chunks[0] == filler, "the first thing the callee heard was not the filler"
    assert audio.total_bytes > len(filler), "the real reply should still follow"
    assert "playing a filler" in caplog.text


class _StallsAfterFirstCall(MockTextToSpeech):
    """Fast for the filler synthesised at call start, then stalls on every reply."""

    def first_audio_delay(self, call_index: int) -> float:
        return 0.0 if call_index == 0 else 0.4


def test_tts_stall_still_gets_a_filler_from_the_cache() -> None:
    audio = RecordingAudioOut()
    pipeline = CallPipeline(
        stt=ScriptedSpeechToText(["hello there"], word_delay_s=0.02),
        llm=MockLanguageModel(),
        tts=_StallsAfterFirstCall(),
        audio_out=audio,
        speculate=False,
        stall_filler_s=0.1,
    )
    [turn] = asyncio.run(pipeline.run())

    assert turn.filler_played
    assert audio.chunks[0] == pipeline._filler_audio


def test_no_filler_when_the_reply_is_on_time() -> None:
    audio = RecordingAudioOut()
    pipeline = CallPipeline(
        stt=ScriptedSpeechToText(["hello there"], word_delay_s=0.02),
        llm=MockLanguageModel(),
        tts=MockTextToSpeech(),
        audio_out=audio,
        speculate=False,
        stall_filler_s=0.5,
    )
    [turn] = asyncio.run(pipeline.run())

    assert not turn.filler_played
    # Only the reply went out on the line: no extra filler bytes.
    assert audio.total_bytes == turn.audio_bytes > 0


def test_thresholds_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLOW_TURN_MS", "750")
    monkeypatch.setenv("STALL_FILLER_MS", "1200")
    pipeline = _pipeline()
    assert pipeline.slow_turn_ms == 750.0
    assert pipeline.stall_filler_s == pytest.approx(1.2)
