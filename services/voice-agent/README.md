# voice-agent

The real-time call loop: Deepgram STT → GPT-4.1 (tool calling) → Cartesia TTS,
streaming end to end.

- `pipeline.py` — the async turn loop. Vendor clients are behind small
  interfaces; the versions here are mocks, so it runs with no API keys. Each
  turn is wrapped in a stopwatch that logs an STT / model / TTS breakdown
  against the 300 ms median target.
- `tools.py` — `send_dtmf` and `end_call`, the two reference tools that pin the
  contract (JSON Schema + validating handler + registry) for the full 17-tool
  layer.

```bash
python services/voice-agent/pipeline.py
```
