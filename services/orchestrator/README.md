# orchestrator

Task fan-out, the call state machine, and outcome consolidation.

- `dispatcher.py` — places the call for a confirmed task through the
  `Telephony` interface, with a signed media-stream token, and registers the
  call context (task, details, user name and location, signed report token).
- `notifier.py` — WhatsApp status updates: one outcome message per call
  (summary and transcript on success, failure type otherwise) and a single
  "still on the line" update once a call passes `EXPECTED_CALL_WINDOW_S`.
  Tasks with `max_attempts` > 1 (calls to stores the assistant found) are
  redialled on busy / no answer / failed after `CALL_RETRY_DELAY_S`.
- `state_machine.py` — `CallState` transitions are declared explicitly;
  anything not declared raises `InvalidTransition`. `failed → queued` is the
  only retry edge, bounded by `MAX_ATTEMPTS_PER_NUMBER`. `TaskRun.consolidate`
  collapses every attempt into one `(succeeded, summary)` pair.
