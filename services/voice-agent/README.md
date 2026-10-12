# voice-agent

The real-time call loop: Deepgram STT → GPT-4.1 → Cartesia TTS, streaming end
to end.

- `pipeline.py` — `CallPipeline`, the async turn loop. Interim transcripts start
  speculative model requests so the model is working before the callee stops
  talking; model tokens are chunked into phrases and streamed into TTS while
  the model is still generating; `aclose()` releases every stream, timer and
  socket however the call ends. Each turn logs an STT / model / TTS breakdown.
- `vendors/` — `base.py` holds the protocols; `deepgram.py`, `openai_llm.py`
  and `cartesia.py` are the live adapters; `mock.py` holds in-memory stand-ins;
  `factory.py` picks live or mock per stage from the environment (mock when
  the key is unset or a placeholder).
- `telephony.py` — the telephony provider boundary (Twilio + mock).
- `tools.py` — `send_dtmf` and `end_call`, the reference tools that pin the
  tool-layer contract.

```bash
python services/voice-agent/pipeline.py   # offline demo, no keys needed
```
