# eval

Scenario-based evaluation for the voice agent.

```bash
python eval/harness.py                                  # every scenario
python eval/harness.py eval/scenarios/restaurant_booking.yaml
```

A scenario is a YAML file in `scenarios/`: an instruction, a list of callee
turns with optional `expect` checkpoints, and an `expected_outcome`. The
harness replays it against a stub agent and reports pass/fail plus a latency
score — the share of turns inside `latency_budget_ms` (default 300).

Swap `StubAgent` for a client that drives `services/voice-agent` once the live
pipeline is wired up; the report contract does not change.
