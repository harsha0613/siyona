# Architecture

## Services

| Service | Responsibility | Key module |
| --- | --- | --- |
| `whatsapp-gateway` | Verify the inbound HMAC signature, parse the message envelope, create a Task, hold per-user session state | `webhook.py` |
| `orchestrator` | Fan a task out across candidate numbers, drive the call state machine, consolidate results into one outcome | `state_machine.py` |
| `voice-agent` | The real-time STT → LLM → TTS loop and the tool layer the model calls during a call | `pipeline.py`, `tools.py` |
| `api` | Accounts, tasks, calls, transcripts, outcomes, retention | `models.py` |
| `web` | React review dashboard over tasks and transcripts | `src/App.tsx` |

Services are separate because they have different failure and scaling
characteristics: the gateway is bursty and stateless, the voice agent is a
long-lived stateful socket per call, and the API is ordinary CRUD.

## Request flow

1. WhatsApp POSTs to `/webhook/whatsapp`. The gateway recomputes the
   `X-Hub-Signature-256` HMAC over the raw body and compares it in constant
   time. A bad or missing signature is a 401 and nothing is created.
2. A valid text message becomes a `Task` (status `received`). Status callbacks
   and non-text messages are acknowledged and ignored, not retried.
3. The orchestrator fans the task out into one or more `CallAttempt`s, each
   moving through `queued → dialing → in_ivr → connected → completed`. Illegal
   transitions raise rather than silently corrupting state; `failed → queued` is
   the only retry edge, bounded by `MAX_ATTEMPTS_PER_NUMBER`.
4. The voice agent opens a media stream and runs the turn loop below.
5. On `end_call`, the orchestrator consolidates every attempt into one
   `Outcome`, which the gateway sends back to the user on WhatsApp.

## The turn loop

```
callee audio ──▶ Deepgram socket ──▶ interim transcripts ──▶ model (warm)
                                  └─▶ final transcript ────▶ model (commit)
                                                              │
                                              token stream ───┤
                                                              ▼
                                                     Cartesia stream
                                                              │
                                                              ▼
                                                      audio to callee
```

Interim transcripts are fed to the model as they arrive so it can plan before
the caller stops speaking; only the final transcript commits a turn. Tokens are
handed to TTS as they are produced rather than after the full reply, so audio
starts before the sentence is finished.

`StageTimings` wraps each stage in a stopwatch and logs
`stt / model / tts / total` per turn, flagging any turn over the budget.

### Latency budget

| Stage | Budget | Where it goes |
| --- | --- | --- |
| STT finalisation | ~80 ms | Endpointing after the callee stops |
| Model first token | ~120 ms | Prompt is kept small; history is summarised |
| TTS first audio | ~80 ms | Streaming synthesis, no full-utterance wait |
| Network / jitter | ~20 ms | Same-region vendors |
| **Total (median)** | **≤ 300 ms** | |

## Tool layer

Every tool is a `Tool`: a name, a description, a JSON Schema, and a pure
handler. Schemas set `additionalProperties: false` and give every property a
type and a description, so a schema test can enforce the contract uniformly
across all 17 tools. Handlers validate their own arguments and raise
`ToolError` rather than trusting the model. Dispatch is by name through
`TOOL_REGISTRY`.

## Data model

```
Task 1──* Call 1──* Transcript
  └──1 Outcome
```

`Task` carries the instruction, status enum and retention window. `Call` is one
attempt at one number and records its median turn latency. `Transcript` is one
speech turn with a speaker and its latency. `Outcome` is the single consolidated
result reported to the user. Deleting a task cascades to everything below it,
which is how retention is enforced.

## Trust and safety

- The development HMAC secret is an obvious non-production string; real
  deployments must set `WHATSAPP_APP_SECRET`.
- Call duration is capped, so a confused agent cannot hold someone's line.
- Transcripts and recordings default to a 30-day retention and are deleted by
  cascade, not by a best-effort sweep.
- The agent states that it is an assistant calling on a user's behalf.
