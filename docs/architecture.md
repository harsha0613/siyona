# Architecture

## Services

| Service | Responsibility | Key module |
| --- | --- | --- |
| `whatsapp-gateway` | Verify the inbound HMAC signature, parse the message envelope, create a Task, hold per-user session state, send replies | `webhook.py`, `whatsapp.py` |
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
   time. A bad or missing signature is a 403 and nothing is created or sent.
2. A valid text message becomes a `Task` (status `received`) holding the raw
   text and the sender. A message we can't act on (non-text, empty, missing
   fields) is logged and the sender gets a clarification prompt. Delivery and
   read receipts carry no message and are acknowledged without a reply. A
   malformed envelope is logged and rejected with a 400.
3. **Confirmation gate.** Intake parses the text into who / what / when (numbers
   only ever come from the user's own words) and sends that summary back. The
   task waits in `awaiting_confirmation`: YES moves it to `in_progress` and the
   dispatcher places the call through the `Telephony` interface; NO, or no
   reply within `CONFIRMATION_TIMEOUT_S`, cancels it and nothing is dialled. A
   request with no number waits in `needs_user_input` until the user sends one.
4. The orchestrator fans the task out into one or more `CallAttempt`s, each
   moving through `queued → dialing → in_ivr → connected → completed`. Illegal
   transitions raise rather than silently corrupting state; `failed → queued` is
   the only retry edge, bounded by `MAX_ATTEMPTS_PER_NUMBER`.
5. The voice agent opens a media stream and runs the turn loop below.
6. On `end_call`, the orchestrator consolidates every attempt into one
   `Outcome`, which the gateway sends back to the user on WhatsApp.

### Status updates

The `StatusNotifier` (`services/orchestrator/notifier.py`) sends exactly one
end-of-call message per task: the agent's plain-language summary on success, or
the failure type (no answer / busy / hung up / couldn't connect). Outcomes come
from the voice agent (success + summary) and from the vendor's status callback
at `POST /telephony/status`, which the `Telephony` adapter verifies and
translates into a vendor-neutral `CallOutcome`; a forged callback is a 403.
If a call is still running `EXPECTED_CALL_WINDOW_S` after dialling, the user
gets one "still on the line" update.

## Telephony provider boundary

The telephony vendor sits behind one interface in
`services/voice-agent/telephony.py`. Nothing outside that module knows which
vendor places the call.

```
orchestrator ──place_call(to, call_id, stream_token)──▶ Telephony (Protocol)
             ◀────────────── provider call id ────────┤
             ──hang_up(provider_call_sid)────────────▶ │
                                                       ├── TwilioTelephony  (TELEPHONY_* set)
                                                       └── MockTelephony    (default, no keys)
```

| Crosses the boundary | Stays inside the adapter |
| --- | --- |
| E.164 number to dial, our `call_id` | Vendor REST endpoints, auth, payload format |
| A signed, expiring media-stream token | TwiML / call-control markup |
| The provider's call id (opaque string) | Vendor status-callback and signature schemes |
| `TelephonyError` on failure | Vendor-specific error codes |

Rules for an adapter:

- `place_call` returns as soon as the vendor accepts the call; progress arrives
  later through the vendor's status callbacks.
- `hang_up` on a call that is already over is a no-op, not an error.
- The answered call's audio must be bridged to `/twilio/media` (or the vendor's
  equivalent) carrying `call_id` and the stream token. The voice service rejects
  any stream whose token fails `signing.verify(..., "media-stream", ...)`.
- `build_telephony()` chooses the adapter from the environment. With no
  credentials it returns `MockTelephony`, so CI and local runs never dial.

Swapping vendors means adding one adapter class and one branch in
`build_telephony()`; the orchestrator and voice pipeline do not change.

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
