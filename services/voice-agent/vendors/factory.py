"""Pick a vendor per stage: the live adapter when its key is set, otherwise the mock.

Each stage is chosen independently, so you can, say, run real Deepgram against
the mock model. With no keys at all the whole pipeline is in-memory, which is
what CI and local runs use.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from vendors.base import LanguageModel, SpeechToText, TextToSpeech
from vendors.mock import MockLanguageModel, MockTextToSpeech, ScriptedSpeechToText


def _key(name: str) -> str:
    value = os.environ.get(name, "")
    return "" if value.startswith("your-") else value


@dataclass(frozen=True)
class VoiceVendorConfig:
    deepgram_api_key: str = ""
    openai_api_key: str = ""
    cartesia_api_key: str = ""
    stt_model: str = "nova-3"
    llm_model: str = "gpt-4.1"
    tts_model: str = "sonic-2"
    tts_voice_id: str = ""
    cartesia_version: str = "2024-06-10"
    language: str = "en"

    @classmethod
    def from_env(cls) -> VoiceVendorConfig:
        return cls(
            deepgram_api_key=_key("DEEPGRAM_API_KEY"),
            openai_api_key=_key("OPENAI_API_KEY"),
            cartesia_api_key=_key("CARTESIA_API_KEY"),
            stt_model=os.environ.get("STT_MODEL", "nova-3"),
            llm_model=os.environ.get("LLM_MODEL", "gpt-4.1"),
            tts_model=os.environ.get("TTS_MODEL", "sonic-2"),
            tts_voice_id=os.environ.get("TTS_VOICE_ID", ""),
            cartesia_version=os.environ.get("CARTESIA_VERSION", "2024-06-10"),
            language=os.environ.get("CALL_LANGUAGE", "en"),
        )


@dataclass
class Vendors:
    stt: SpeechToText
    llm: LanguageModel
    tts: TextToSpeech


def build_vendors(
    config: VoiceVendorConfig | None = None, mock_script: list[str] | None = None
) -> Vendors:
    """Construct (but don't connect) the three vendors. The pipeline starts and closes them."""
    config = config or VoiceVendorConfig.from_env()

    stt: SpeechToText
    if config.deepgram_api_key:
        from vendors.deepgram import DeepgramSpeechToText

        stt = DeepgramSpeechToText(
            config.deepgram_api_key, model=config.stt_model, language=config.language
        )
    else:
        stt = ScriptedSpeechToText(mock_script or [])

    llm: LanguageModel
    if config.openai_api_key:
        from openai import AsyncOpenAI

        from vendors.openai_llm import OpenAIChatModel

        client = AsyncOpenAI(api_key=config.openai_api_key, timeout=10.0, max_retries=1)
        llm = OpenAIChatModel(client, config.llm_model)
    else:
        llm = MockLanguageModel()

    tts: TextToSpeech
    if config.cartesia_api_key and config.tts_voice_id not in ("", "default"):
        from vendors.cartesia import CartesiaTextToSpeech

        tts = CartesiaTextToSpeech(
            config.cartesia_api_key,
            version=config.cartesia_version,
            model_id=config.tts_model,
            voice_id=config.tts_voice_id,
            language=config.language,
        )
    else:
        tts = MockTextToSpeech()

    return Vendors(stt, llm, tts)
