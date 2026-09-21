# Siyona

A WhatsApp-based agentic AI voice assistant that places real outbound phone
calls on a user's behalf. Send it an instruction on WhatsApp — *"book a table
for four at Bella Vista tonight at 7pm"* — and Siyona dials the number,
navigates the IVR, holds a natural conversation with whoever picks up, and
reports back an outcome plus the full transcript.

> **Status: scaffold.** This is an Assignment-3 deliverable. The structure,
> contracts and CI are real; the vendor integrations are mocked so everything
> here runs with no API keys.

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

The voice agent is a fully streaming loop. Deepgram emits interim transcripts
that warm the model before the caller finishes speaking; the model streams
tokens straight into Cartesia, which streams audio back onto the call. Every
turn is wrapped in a stopwatch that logs an STT / model / TTS breakdown against
a **median turn latency target of ≤ 300 ms**.

Read more in [docs/architecture.md](docs/architecture.md).

## Repository layout

| Path | What lives there |
| --- | --- |
| `services/whatsapp-gateway/` | Inbound webhook, HMAC signature check, session state |
| `services/voice-agent/` | Streaming STT → LLM → TTS pipeline and the tool layer |
| `services/orchestrator/` | Task fan-out, call state machine, outcome consolidation |
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
