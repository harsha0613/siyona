# Siyona

A WhatsApp-based agentic AI voice assistant that places real outbound phone
calls on a user's behalf. Send it an instruction on WhatsApp — *"book a table
for four at Bella Vista tonight at 7pm"* — and Siyona dials the number,
navigates the IVR, holds a natural conversation with whoever picks up, and
reports back an outcome plus the full transcript.

> **Status: Sprint 1 implemented.** The Sprint 1 stories are implemented:
>
> - **WhatsApp intake**: HMAC-verified webhook that turns a message into a task
>   (bad signature → 403, unreadable message → clarification prompt).
> - **Confirmation gate**: a who / what / when summary that the user must
>   confirm before anything is dialled. NO or timeout cancels it.
> - **Status updates**: an outcome message when the call completes or fails
>   (no answer / busy / hung up), plus a "still on the line" update when a call
>   runs long.
> - **Streaming voice pipeline**: Deepgram → GPT-4.1 → Cartesia, with partial
>   transcripts reaching the model early and TTS streaming as the model writes.
> - **Latency instrumentation**: per-turn STT / model / TTS breakdown, slow-turn
>   warnings over 500 ms, and a filler instead of silence on a vendor stall.
>
> Every vendor (telephony, WhatsApp, STT, LLM, TTS) sits behind an interface
> and falls back to an in-memory mock when its key is unset. Local runs and CI
> need no API keys.

## Architecture

```
WhatsApp  ──▶  whatsapp-gateway  ──▶  orchestrator  ──▶  voice-agent  ──▶  PSTN
                (HMAC verify,          (task fan-out,      (Deepgram STT
                 session state)         call state          → GPT-4.1 tools
                      │                 machine)            → Cartesia TTS)
                      ▼                      │                   │
                     api  ◀───────────────────┴───────────────────┘
              (accounts, tasks, transcripts, retention)
                      │
                      ▼
                     web  (React review dashboard)
```

The voice agent is a fully streaming loop. Deepgram's interim transcripts start
speculative model requests before the caller finishes speaking; the model
streams tokens straight into Cartesia, which streams audio back onto the call.
Every turn is wrapped in a stopwatch that logs an STT / model / TTS breakdown
against a **median turn latency target of ≤ 300 ms**, and turns over 500 ms are
flagged with their stage breakdown.

Read more in [docs/architecture.md](docs/architecture.md).

## Repository layout

| Path | What lives there |
| --- | --- |
| `services/whatsapp-gateway/` | Inbound webhook, HMAC signature check, intake, confirmation gate |
| `services/voice-agent/` | Streaming STT → LLM → TTS pipeline, vendor adapters, latency instrumentation, telephony interface |
| `services/orchestrator/` | Call dispatch, WhatsApp status updates, call state machine |
| `services/api/` | Accounts, tasks, transcripts, retention (SQLAlchemy models) |
| `web/` | React + TypeScript review dashboard |
| `eval/` | Scenario files and the evaluation harness |
| `infra/` | docker compose stack and Terraform scaffold |
| `docs/` | Vision, roadmap, release plan, architecture |

## Local setup

```bash
git clone https://github.com/harsha0613/siyona.git
cd siyona

python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

cp .env.example .env          # placeholders are fine; the mocks need no keys
```

Bring up the whole stack (Postgres, Redis, and the Python services):

```bash
docker compose -f infra/docker-compose.yml up --build
# gateway on http://localhost:8000  (GET /healthz)
```

Run the review dashboard:

```bash
cd web && npm install && npm run dev     # http://localhost:5173
```

## Running things

```bash
# The streaming voice pipeline against mock vendors, with latency breakdowns
python services/voice-agent/pipeline.py

# Tests
pytest

# Lint and format
ruff check .
ruff format --check .

# Evaluation harness — every scenario, or one file
python eval/harness.py
python eval/harness.py eval/scenarios/restaurant_booking.yaml
```

The harness replays a YAML scenario against a stub agent, checks each required
checkpoint and the expected outcome, and prints a pass/fail plus a latency
score (the share of turns inside the per-turn budget).

## WhatsApp assistant tools

Messages that don't carry a phone number go to the WhatsApp assistant: a
tool-calling model loop (`services/whatsapp-gateway/assistant.py`) that finds
real businesses instead of guessing. Its prompt forbids made-up business names,
addresses, phone numbers and ratings; every business it mentions must come from
a tool result. It is on when `OPENAI_API_KEY` is set; without it the gateway
keeps the rule-based intake. Maps calls go through `maps.py`, which falls back
to `MockMaps` (finds nothing) while `GOOGLE_MAPS_API_KEY` is unset.

### `set_user_location`

Saves the user's address so "near me" searches have an origin. Needs no key.

### `google_places`

Google Places Text Search around the saved address (or an area the user
names). Returns up to five results with name, address, rating and open-now.
The place ids are kept in the user's session for follow-ups and never shown to
the model or the user. Requires `GOOGLE_MAPS_API_KEY`.

```
User:    I'm at 950 Main St, Worcester MA. Find a pharmacy near me
Siyona:  Here's what I found near you:
         1. Main Street Pharmacy (4.6★, open now)
         2. Park Avenue Drug (4.1★)
```

### `place_details`

Phone number, address, website, opening hours, rating, price level, up to
three reviews and the Google Maps link for a place from the last search.
"The first one", "#2" or "tell me more about Park Avenue Drug" resolve to the
stored place id; an ambiguous or unknown name is refused rather than guessed.
Any field Google doesn't have comes back as "Not available", never an invented
value. Requires `GOOGLE_MAPS_API_KEY`.

```
User:    Tell me more about the first one
Siyona:  Main Street Pharmacy, 12 Main St, Worcester MA
         📞 (508) 555-0142
         Mon–Sat 8:00 AM – 9:00 PM, Sun closed · 4.6★ · $$
User:    And Park Avenue Drug's number?
Siyona:  Their phone number is not available on Google.
```

### `create_call_request`

Calls the store from the last `place_details` lookup on the user's behalf. The
model never supplies a phone number: the tool dials the number Google lists for
that place, and refuses if there isn't one. Before it runs, the assistant asks
the 2-3 key questions for that kind of business. The call then goes through the
same confirmation gate as a typed number (nothing is dialled until YES) and the
Sprint 1 telephony interface and streaming pipeline.

- The call context (task, the user's answers, their WhatsApp name and saved
  address) is registered under the call id; the call opens with an AI
  disclosure naming the user.
- Busy, no answer or failed: the same task is redialled to the same number
  after `CALL_RETRY_DELAY_S`, up to `MAX_ATTEMPTS_PER_NUMBER` attempts, and the
  user is told each time.
- The voice agent posts its outcome and transcript to `POST /calls/report`
  (signed per call); the user gets the summary and the transcript on WhatsApp.
- "Yes, book it" after a result places a confirmed follow-up call to the same
  store with the updated details.

Requires `GOOGLE_MAPS_API_KEY`, `OPENAI_API_KEY` and the `TELEPHONY_*`
variables (the mock telephony is used without them).

```
User:    Call the first one and ask if they have amoxicillin
Siyona:  Before I call: is the prescription under Asha K, and what strength?
User:    Yes, 500 mg, pickup
Siyona:  Before I call, please check:
         • Who: Main Street Pharmacy on +1 508-555-0142
         • What: Ask if they have amoxicillin 500 mg in stock for pickup
         • I'll tell them: Customer name: Asha K. Medication: amoxicillin 500 mg.
         • When: as soon as possible
         Reply YES to call or NO to cancel.
User:    YES
Siyona:  📞 Calling now. I'll message you when I'm done.
Siyona:  ✅ Done. Main Street Pharmacy has amoxicillin 500 mg in stock.
Siyona:  📝 Transcript of Main Street Pharmacy: ...
User:    Great, yes, book it for 5pm
Siyona:  Before I call, please check: ...
```

## Tech stack

| Layer | Choice | Why |
| --- | --- | --- |
| Speech-to-text | Deepgram (`nova-3`, streaming) | Interim transcripts let the model start planning mid-utterance |
| Reasoning | GPT-4.1 with tool calling | Strong instruction following; 17-tool layer for call control |
| Text-to-speech | Cartesia (streaming) | Sub-100 ms time-to-first-audio |
| Telephony | Programmable voice provider (media streams) | Raw bidirectional audio, DTMF injection |
| Messaging | WhatsApp Business Cloud API | Where the user already is; no app to install |
| Services | Python 3.11, FastAPI, asyncio | One async story from socket to socket |
| Data | PostgreSQL + SQLAlchemy 2.0, Redis | Durable task/transcript history; Redis for live session state |
| Dashboard | React 18 + TypeScript + Vite | Fast review UI for transcripts and outcomes |
| Infra | Docker Compose now, Terraform (AWS) next | Local parity first, cloud in R2 |
| Quality | pytest, ruff, GitHub Actions | Lint, tests and a docker build on every push and PR |

## Secrets

No real credentials live in this repository. `.env.example` holds placeholder
values only, `.env` is gitignored, and the gateway's development HMAC secret is
an obvious non-production string that must be overridden by
`WHATSAPP_APP_SECRET` in any real deployment.

## Documentation

- [docs/vision.md](docs/vision.md) — the problem, the user, the bet
- [docs/roadmap.md](docs/roadmap.md) — Now / Next / Later across Themes A, B, C
- [docs/release-plan.md](docs/release-plan.md) — R1–R3 with acceptance gates
- [docs/architecture.md](docs/architecture.md) — services, data flow, latency budget

## License

[MIT](LICENSE)
