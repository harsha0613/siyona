"""Smoke tests for the streaming pipeline, the data model, and the eval harness."""

from __future__ import annotations

import asyncio
from pathlib import Path

from harness import Scenario, run_scenario
from models import Call, CallStatus, Outcome, Speaker, Task, TaskStatus, Transcript, engine_for
from pipeline import DEMO_UTTERANCES, demo_pipeline
from sqlalchemy.orm import Session
from state_machine import CallState, InvalidTransition, TaskRun

SCENARIO = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "restaurant_booking.yaml"


def test_pipeline_produces_one_turn_per_final_transcript() -> None:
    async def run():
        async with demo_pipeline(DEMO_UTTERANCES) as pipeline:
            return await pipeline.run(), pipeline

    turns, pipeline = asyncio.run(run())

    assert len(turns) == len(DEMO_UTTERANCES)
    assert [turn.caller_text for turn in turns] == DEMO_UTTERANCES
    assert all(turn.audio_bytes > 0 for turn in turns)
    assert all(turn.timings.total_ms > 0 for turn in turns)
    # Interim transcripts reached the model before the final one did.
    assert pipeline.llm.seen_partials
    assert pipeline.stt.closed


def test_stage_timings_sum_to_total() -> None:
    async def run():
        async with demo_pipeline(["for billing press one"]) as pipeline:
            return await pipeline.run()

    timings = asyncio.run(run())[0].timings
    stages = timings.stt_ms + timings.model_ms + timings.tts_ms

    assert timings.total_ms == stages
    assert timings.within_budget is (timings.total_ms <= 300.0)


def test_task_call_transcript_outcome_relationships() -> None:
    engine = engine_for()
    with Session(engine) as session:
        task = Task(user_phone="15551230000", instruction="Book a table for four")
        call = Call(callee_number="+15551234567", status=CallStatus.CONNECTED)
        call.transcripts = [
            Transcript(turn_index=0, speaker=Speaker.CALLEE, text="Bella Vista, hello?"),
            Transcript(turn_index=1, speaker=Speaker.AGENT, text="A table for four at 7pm."),
        ]
        task.calls.append(call)
        task.outcome = Outcome(succeeded=True, summary="Booked for four at 7pm.")
        task.status = TaskStatus.COMPLETED
        session.add(task)
        session.commit()

        stored = session.get(Task, task.id)
        assert stored is not None
        assert stored.status is TaskStatus.COMPLETED
        assert len(stored.calls) == 1
        assert [t.speaker for t in stored.calls[0].transcripts] == [Speaker.CALLEE, Speaker.AGENT]
        assert stored.outcome is not None and stored.outcome.succeeded
        assert stored.calls[0].task.id == stored.id


def test_deleting_a_task_cascades_to_calls_and_transcripts() -> None:
    engine = engine_for()
    with Session(engine) as session:
        task = Task(user_phone="15551230000", instruction="Call the dentist")
        task.calls.append(
            Call(
                callee_number="+15559876543",
                transcripts=[Transcript(turn_index=0, speaker=Speaker.SYSTEM, text="dialing")],
            )
        )
        session.add(task)
        session.commit()

        session.delete(task)
        session.commit()

        assert session.query(Call).count() == 0
        assert session.query(Transcript).count() == 0


def test_call_state_machine_allows_the_happy_path() -> None:
    run = TaskRun.fan_out("task-1", "Book a table", ["+1555000001"])
    call = run.calls[0]
    for state in (CallState.DIALING, CallState.IN_IVR, CallState.CONNECTED, CallState.COMPLETED):
        call.transition(state)

    call.summary = "Booked for four at 7pm"
    assert run.settled
    succeeded, summary = run.consolidate()
    assert succeeded and "+1555000001" in summary


def test_call_state_machine_rejects_illegal_transitions() -> None:
    call = TaskRun.fan_out("task-2", "Call back", ["+1555000002"]).calls[0]
    try:
        call.transition(CallState.COMPLETED)
    except InvalidTransition:
        pass
    else:  # pragma: no cover - guards against a silently permissive machine
        raise AssertionError("queued -> completed should not be allowed")


def test_harness_scores_the_example_scenario() -> None:
    report = run_scenario(Scenario.from_file(SCENARIO))

    assert report.passed
    assert report.outcome == "task_complete"
    assert report.latency_score == 1.0
    assert len(report.turns) == 5
