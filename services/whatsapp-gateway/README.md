# whatsapp-gateway

Inbound WhatsApp handling and per-user session state.

- `webhook.py` — `POST /webhook/whatsapp` verifies the `X-Hub-Signature-256`
  HMAC over the raw body, parses the Cloud API envelope, and creates a Task
  stub. Bad or missing signature → `401`. Status callbacks → `200 ignored`.

```bash
uvicorn webhook:app --app-dir services/whatsapp-gateway --reload
```

The task store is an in-process dict for now; R2 replaces it with Redis for
session state plus a write-through to the API service.
