"""Scenario harness for the Siyona voice agent.

Loads a YAML scenario from ``eval/scenarios/``, replays its caller turns
against a stub agent, and scores the run on two axes:

* **correctness** - did the agent hit every required checkpoint and reach the
  expected outcome?
* **latency** - what share of turns landed inside the per-turn budget?

Usage::

    python eval/harness.py                              # run every scenario
    python eval/harness.py eval/scenarios/restaurant_booking.yaml
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
DEFAULT_LATENCY_BUDGET_MS = 300.0


@dataclass
class ScenarioTurn:
    callee_says: str
    expect: str | None = None


@dataclass
class Scenario:
    name: str
    description: str
    callee_number: str
    instruction: str
    turns: list[ScenarioTurn]
    expected_outcome: str
    latency_budget_ms: float = DEFAULT_LATENCY_BUDGET_MS
    path: Path | None = None

    @classmethod
    def from_file(cls, path: Path) -> Scenario:
        data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        missing = {"name", "instruction", "turns", "expected_outcome"} - set(data)
        if missing:
            raise ValueError(f"{path.name}: missing keys {sorted(missing)}")
        turns = [
            ScenarioTurn(callee_says=turn["callee_says"], expect=turn.get("expect"))
            for turn in data["turns"]
        ]
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            callee_number=data.get("callee_number", "+15550000000"),
            instruction=data["instruction"],
            turns=turns,
            expected_outcome=data["expected_outcome"],
            latency_budget_ms=float(data.get("latency_budget_ms", DEFAULT_LATENCY_BUDGET_MS)),
            path=path,
        )


@dataclass
class StubAgent:
    """Deterministic stand-in for the real agent, used to exercise the harness.

    Replace with a client that drives ``services/voice-agent`` once the live
    pipeline is wired up; the harness contract stays the same.
    """

    instruction: str
    seed: int = 7
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def respond(self, callee_says: str) -> tuple[str, float]:
        """Return ``(agent_reply, turn_latency_ms)`` for one callee utterance."""
        lowered = callee_says.lower()
        if "press" in lowered:
            reply = "send_dtmf(1) Selecting that option."
        elif "how many" in lowered or "party" in lowered:
            reply = "A table for four people, please."
        elif "what time" in lowered or "when" in lowered:
            reply = "Seven o'clock this evening would be ideal."
        elif "name" in lowered:
            reply = "The booking is under Siyona."
        elif "confirm" in lowered or "booked" in lowered:
            reply = "end_call(task_complete) Thank you, that is confirmed."
        else:
            reply = "Understood, thank you."
        return reply, self._rng.uniform(180.0, 340.0)


@dataclass
class TurnReport:
    callee_says: str
    agent_reply: str
    latency_ms: float
    expected: str | None
    matched: bool

    @property
    def ok(self) -> bool:
        return self.expected is None or self.matched


@dataclass
class ScenarioReport:
    scenario: Scenario
    turns: list[TurnReport]
    outcome: str

    @property
    def checkpoints_passed(self) -> bool:
        return all(turn.ok for turn in self.turns)

    @property
    def outcome_passed(self) -> bool:
        return self.outcome == self.scenario.expected_outcome

    @property
    def passed(self) -> bool:
        return self.checkpoints_passed and self.outcome_passed

    @property
    def latency_score(self) -> float:
        """Share of turns inside the budget, 0.0-1.0."""
        if not self.turns:
            return 1.0
        budget = self.scenario.latency_budget_ms
        within = sum(1 for turn in self.turns if turn.latency_ms <= budget)
        return within / len(self.turns)

    @property
    def median_latency_ms(self) -> float:
        if not self.turns:
            return 0.0
        ordered = sorted(turn.latency_ms for turn in self.turns)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2


def run_scenario(scenario: Scenario, agent: StubAgent | None = None) -> ScenarioReport:
    """Replay a scenario against an agent and collect a report."""
    agent = agent or StubAgent(instruction=scenario.instruction)
    turns: list[TurnReport] = []
    outcome = "incomplete"

    for turn in scenario.turns:
        reply, latency_ms = agent.respond(turn.callee_says)
        matched = turn.expect is not None and turn.expect.lower() in reply.lower()
        turns.append(
            TurnReport(
                callee_says=turn.callee_says,
                agent_reply=reply,
                latency_ms=latency_ms,
                expected=turn.expect,
                matched=matched,
            )
        )
        if "end_call(task_complete)" in reply:
            outcome = "task_complete"
        elif "end_call(task_failed)" in reply:
            outcome = "task_failed"

    return ScenarioReport(scenario=scenario, turns=turns, outcome=outcome)


def print_report(report: ScenarioReport) -> None:
    verdict = "PASS" if report.passed else "FAIL"
    print(f"\n[{verdict}] {report.scenario.name}")
    if report.scenario.description:
        print(f"       {report.scenario.description}")
    for idx, turn in enumerate(report.turns, start=1):
        mark = "ok " if turn.ok else "MISS"
        budget = "" if turn.latency_ms <= report.scenario.latency_budget_ms else " (over budget)"
        print(f"  {idx:>2}. {mark} {turn.latency_ms:6.1f} ms{budget}  {turn.agent_reply}")
        if not turn.ok:
            print(f"       expected to contain: {turn.expected!r}")
    print(f"  outcome: {report.outcome} (expected {report.scenario.expected_outcome})")
    print(
        f"  latency score: {report.latency_score:.0%} of turns within "
        f"{report.scenario.latency_budget_ms:.0f} ms, median {report.median_latency_ms:.1f} ms"
    )


def discover_scenarios(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(path) for path in paths]
    return sorted(SCENARIO_DIR.glob("*.yaml"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Siyona evaluation scenarios.")
    parser.add_argument("scenarios", nargs="*", help="scenario YAML files (default: all)")
    args = parser.parse_args(argv)

    files = discover_scenarios(args.scenarios)
    if not files:
        print(f"no scenarios found in {SCENARIO_DIR}", file=sys.stderr)
        return 1

    reports = [run_scenario(Scenario.from_file(path)) for path in files]
    for report in reports:
        print_report(report)

    passed = sum(1 for report in reports if report.passed)
    score = sum(report.latency_score for report in reports) / len(reports)
    print(f"\n{passed}/{len(reports)} scenarios passed, mean latency score {score:.0%}")
    return 0 if passed == len(reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
