# Release plan

Three releases. Each has an explicit acceptance gate; nothing moves to the next
release until every gate item is green and demonstrable.

## R1 — Walking skeleton (mocked vendors)

**Goal:** every seam in the system exists and is exercised by CI. No real call
is placed.

Scope:
- Streaming pipeline against mock Deepgram / model / Cartesia clients, with a
  per-turn STT / model / TTS latency breakdown.
- `send_dtmf` and `end_call` tools with JSON schemas and contract tests.
- WhatsApp webhook: HMAC verification, message parsing, Task stub creation.
- Task / Call / Transcript / Outcome models and the call state machine.
- Eval harness with one scenario; dashboard on fixtures.
- docker compose stack; CI running ruff, pytest and a docker build.

**Acceptance gate**
- [ ] `pytest` green; `ruff check .` and `ruff format --check .` clean.
- [ ] CI passes on `main` and on every PR.
- [ ] `docker compose -f infra/docker-compose.yml up --build` brings the stack
      up and `GET /healthz` returns 200.
- [ ] `python eval/harness.py` exits 0 on the example scenario.
- [ ] A bad or missing webhook signature returns 401 and creates no task.
- [ ] No credential, key or real phone number anywhere in the tree.

## R2 — First real call

**Goal:** a real outbound call, end to end, for a single hand-picked task.

Scope:
- Live Deepgram, GPT-4.1 and Cartesia integration behind the existing
  interfaces; telephony provider with bidirectional media streams.
- Full 17-tool layer, including hold and voicemail detection.
- Barge-in and interruption recovery.
- Postgres persistence, live task status back to WhatsApp, dashboard on the
  real API.
- Eval suite grown to ~20 scenarios, run nightly.

**Acceptance gate**
- [ ] Median turn latency ≤ 300 ms and p95 ≤ 600 ms, measured over ≥ 50 live
      turns, with the per-stage breakdown recorded.
- [ ] ≥ 80% of eval scenarios pass; no scenario ends in a fabricated outcome.
- [ ] Ten consecutive real calls complete without a crash or a stuck call; the
      duration cap ends any call that exceeds it.
- [ ] The agent identifies itself as an assistant when asked, in every recorded
      call.
- [ ] Transcripts are persisted, are retrievable in the dashboard, and are
      purged by the retention job once expired.

## R3 — Usable by someone who is not me

**Goal:** a small closed beta can run their own errands without hand-holding.

Scope:
- Multi-turn clarification when an instruction is under-specified.
- Task fan-out across a shortlist of numbers with honest consolidation.
- Accounts, per-user retention settings, and an exportable call record.
- Terraform-deployed cloud environment; CI pushes images to ECR.
- Cost and latency dashboards per task.

**Acceptance gate**
- [ ] ≥ 70% task success rate across ≥ 50 beta tasks from ≥ 5 users.
- [ ] Zero incidents of a reported outcome contradicting its transcript.
- [ ] Median cost per completed task under the agreed budget.
- [ ] `terraform validate` in CI; a deploy to staging is one pipeline run.
- [ ] Documented consent and retention behaviour per supported region.

## Cross-release rules

Every release ships from `main` behind a passing CI run. `main` is protected:
changes land by pull request with CI green. Anything that regresses the latency
budget or the eval pass rate blocks the release rather than shipping with a
caveat.
