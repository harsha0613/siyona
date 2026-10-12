"""Per-turn latency instrumentation.

A :class:`TurnStopwatch` is stamped at four moments in each turn:

    speech_end ──stt──▶ final ──model──▶ first_chunk ──tts──▶ first_audio

which gives three contiguous stages (:class:`StageTimings`) that sum to the
silence the callee actually hears. Every turn is logged with that breakdown;
a turn slower than ``SLOW_TURN_MS`` (default 500 ms) is logged as a warning
naming the slowest stage. At the end of a call, the median and p95 are logged.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

logger = logging.getLogger("siyona.voice_agent.latency")

# Budget for one full turn (callee stops speaking -> first agent audio), median.
TARGET_TURN_LATENCY_MS = 300.0
# Anything slower than this is logged as a slow turn with its stage breakdown.
DEFAULT_SLOW_TURN_MS = 500.0
STAGES = ("stt", "model", "tts")


def slow_turn_ms_from_env() -> float:
    return float(os.environ.get("SLOW_TURN_MS", DEFAULT_SLOW_TURN_MS))


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

    @property
    def slowest_stage(self) -> str:
        return max(STAGES, key=lambda stage: getattr(self, f"{stage}_ms"))

    def as_dict(self) -> dict[str, float]:
        return {
            "stt_ms": round(self.stt_ms, 1),
            "model_ms": round(self.model_ms, 1),
            "tts_ms": round(self.tts_ms, 1),
            "total_ms": round(self.total_ms, 1),
        }


class TurnStopwatch:
    """Stamps the moments of one turn. Only the first stamp of each kind counts."""

    def __init__(
        self, speech_end: float | None, clock: Callable[[], float] = time.perf_counter
    ) -> None:
        self._clock = clock
        self.final = clock()
        self.speech_end = speech_end
        self.first_chunk: float | None = None
        self.first_audio: float | None = None

    def mark_first_chunk(self) -> None:
        if self.first_chunk is None:
            self.first_chunk = self._clock()

    def mark_first_audio(self) -> None:
        if self.first_audio is None:
            self.first_audio = self._clock()

    @property
    def has_audio(self) -> bool:
        return self.first_audio is not None

    def elapsed_ms(self) -> float:
        """Time since the final transcript (what a stall watchdog compares against)."""
        return (self._clock() - self.final) * 1000.0

    def timings(self) -> StageTimings:
        timings = StageTimings()
        if self.speech_end is not None:
            timings.stt_ms = max(0.0, (self.final - self.speech_end) * 1000.0)
        if self.first_chunk is not None:
            timings.model_ms = (self.first_chunk - self.final) * 1000.0
            if self.first_audio is not None:
                timings.tts_ms = (self.first_audio - self.first_chunk) * 1000.0
        return timings


def log_turn(
    timings: StageTimings,
    *,
    turn_index: int,
    slow_turn_ms: float,
    filler_played: bool = False,
    speculative_hit: bool = False,
) -> bool:
    """Log the turn's breakdown. Returns True if the turn was slow."""
    breakdown = timings.as_dict()
    extra = {"turn": turn_index, **breakdown, "filler": filler_played}
    slow = timings.total_ms > slow_turn_ms
    if slow:
        logger.warning(
            "slow turn %d: total=%.1fms > %.0fms (stt=%.1fms model=%.1fms tts=%.1fms, "
            "slowest=%s, filler=%s)",
            turn_index,
            breakdown["total_ms"],
            slow_turn_ms,
            breakdown["stt_ms"],
            breakdown["model_ms"],
            breakdown["tts_ms"],
            timings.slowest_stage,
            filler_played,
            extra={"latency": extra},
        )
    else:
        logger.info(
            "turn %d: stt=%.1fms model=%.1fms tts=%.1fms total=%.1fms budget=%s speculative=%s",
            turn_index,
            breakdown["stt_ms"],
            breakdown["model_ms"],
            breakdown["tts_ms"],
            breakdown["total_ms"],
            "ok" if timings.within_budget else "EXCEEDED",
            speculative_hit,
            extra={"latency": extra},
        )
    return slow


def percentile(values: Sequence[float], pct: float) -> float | None:
    """Nearest-rank percentile; None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def log_call_summary(totals: Sequence[float]) -> None:
    if not totals:
        return
    logger.info(
        "call latency over %d turns: median=%.1fms p95=%.1fms",
        len(totals),
        percentile(totals, 50) or 0.0,
        percentile(totals, 95) or 0.0,
    )
