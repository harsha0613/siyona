# voice-agent

The real-time call loop: Deepgram STT → GPT-4.1 → Cartesia TTS, streaming end
to end.

- `pipeline.py` — `CallPipeline`, the async turn loop. Interim transcripts start
  speculative model requests so the model is working before the callee stops
  talking; model tokens are chunked into phrases and streamed into TTS while
  the model is still generating; `aclose()` releases every stream, timer and
  socket however the call ends. Each turn logs an STT / model / TTS breakdown.
- `latency.py` — per-turn stopwatch: logs the STT / model / TTS breakdown for
  every turn, warns on turns over `SLOW_TURN_MS` (500 ms) naming the slowest
  stage, and logs a median / p95 summary per call. The pipeline plays a cached
  filler if a vendor stalls past `STALL_FILLER_MS`.
- `vendors/` — `base.py` holds the protocols; `deepgram.py`, `openai_llm.py`
  and `cartesia.py` are the live adapters; `mock.py` holds in-memory stand-ins;
  `factory.py` picks live or mock per stage from the environment (mock when
  the key is unset or a placeholder).
- `telephony.py` — the telephony provider boundary (Twilio + mock).
- `call_context.py` — what a call knows (task, the user's details, name and
  location), the AI-disclosure opening line, the call prompt, and `run_call`,
  which runs the pipeline with the call tools and returns the outcome report.
- `tools.py` — `send_dtmf` and `end_call`, the reference tools that pin the
  tool-layer contract.

```bash
python services/voice-agent/pipeline.py   # offline demo, no keys needed
```
