# api

Accounts, tasks, transcripts and retention.

- `models.py` — SQLAlchemy 2.0 models: `Task 1─* Call 1─* Transcript` plus a
  single `Outcome` per task. Deletes cascade, which is how retention is
  enforced. `engine_for()` builds the schema on any URL and defaults to
  in-memory SQLite for tests.

The HTTP layer lands in R2; the models are the contract the dashboard and the
orchestrator already code against.
