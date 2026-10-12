# whatsapp-gateway

Inbound WhatsApp handling and per-user session state.

- `webhook.py` — `POST /webhook/whatsapp` verifies the `X-Hub-Signature-256`
  HMAC over the raw body, parses the Cloud API envelope, and creates a Task
  stub with the raw text and sender. Bad or missing signature → `403`, nothing
  written. A message we can't act on → error logged and a clarification prompt
  sent to the sender. Status callbacks → `200 ignored`.
- `whatsapp.py` — outbound sender. `CloudApiClient` when
  `WHATSAPP_ACCESS_TOKEN` is set, otherwise `MockWhatsApp` (records, never sends).
- `confirmation.py` — the confirmation gate. Every new task gets a who / what /
  when summary; YES → `in_progress` and the call is dialled, NO or timeout →
  `cancelled` with no call.
- `intake.py` / `phone.py` — rule-based parsing of the instruction; numbers come
  only from the user's text (libphonenumber).
- `tasks.py` — the in-memory task store.
- `messages.py` — every user-facing message text in one place.

```bash
uvicorn webhook:app --app-dir services/whatsapp-gateway --reload
```

The task store is an in-process dict for now; R2 replaces it with Redis for
session state plus a write-through to the API service.
