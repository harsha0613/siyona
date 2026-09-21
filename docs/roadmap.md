# Roadmap

Three themes run in parallel. Each is sequenced Now / Next / Later rather than
dated, so the plan survives slippage in any one theme.

| Theme | Question it answers |
| --- | --- |
| **A — Conversation quality** | Does the agent sound and behave like a competent caller? |
| **B — Task reliability** | Does the errand actually get done, and is the report true? |
| **C — Product surface & trust** | Can a real user hand over a task and believe the result? |

## Now

**A. Conversation quality**
- Streaming STT → LLM → TTS loop end to end with per-stage latency logging.
- Barge-in: stop synthesis the moment the callee starts speaking.
- Two reference tools (`send_dtmf`, `end_call`) pinning the tool contract.

**B. Task reliability**
- Call state machine with legal transitions and bounded retries.
- Task → Call → Transcript → Outcome data model with cascade deletes.
- Eval harness with one scripted scenario, scored on checkpoints and latency.

**C. Product surface & trust**
- WhatsApp webhook with HMAC verification and a Task stub.
- Review dashboard rendering tasks, calls and transcripts from fixtures.
- CI on every push and PR: ruff, pytest, docker build.

## Next

**A.** Grow the tool layer to all 17 tools (hold detection, voicemail
detection, transfer handling, spelling-out, number confirmation). Tune the
prompt for interruption recovery and for refusing to invent facts. Get the
median turn inside 300 ms against live vendors, not mocks.

**B.** Live telephony integration and real outbound dialling. Task fan-out
across a shortlist of numbers. Grow the eval suite to ~20 scenarios including
adversarial ones (aggressive callee, wrong number, endless hold). Outcome
consolidation that reports partial success honestly.

**C.** Multi-turn WhatsApp clarification when an instruction is under-specified.
Live call status updates back to the user. Dashboard reads from the API instead
of fixtures. Retention job that actually purges expired transcripts.

## Later

**A.** Multilingual calls and accent robustness. Voice selection. Speculative
response generation off interim transcripts to shave the last tens of
milliseconds.

**B.** Learned IVR maps per callee number, so the second call to a business is
faster than the first. Scheduled and recurring errands. Cost-per-task budgeting
and a cheaper model tier for simple calls.

**C.** Multi-user accounts and delegated calling. Calendar and contacts
integration so the agent can pick the number itself. Public beta, regional
consent handling, and an exportable call record.

## Out of scope for this cycle

Inbound calls, voice cloning of the user, non-WhatsApp front ends, and any bulk
or campaign calling.
