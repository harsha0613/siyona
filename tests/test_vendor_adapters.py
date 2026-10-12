"""Live vendor adapters, exercised against fake sockets/clients (no network, no keys)."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from vendors import cartesia, deepgram
from vendors.base import TextDelta, ToolCallRequest, UsageReport
from vendors.factory import VoiceVendorConfig, build_vendors
from vendors.mock import MockLanguageModel, MockTextToSpeech, ScriptedSpeechToText
from vendors.openai_llm import OpenAIChatModel


class FakeSocket:
    """Enough of a websockets ClientConnection for the adapters."""

    def __init__(self, incoming: list[str] | None = None) -> None:
        self.sent: list[str | bytes] = []
        self.closed = False
        self._incoming: asyncio.Queue[str | None] = asyncio.Queue()
        for message in incoming or []:
            self._incoming.put_nowait(message)
        self.on_send: Callable[[str], None] | None = None

    def push(self, message: dict) -> None:
        self._incoming.put_nowait(json.dumps(message))

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        if self.on_send is not None:
            self.on_send(message)

    async def close(self) -> None:
        self.closed = True
        self._incoming.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._incoming.get()
        if item is None:
            raise StopAsyncIteration
        return item


# --- Deepgram ------------------------------------------------------------------


def _dg_result(text: str, *, is_final: bool, speech_final: bool = False) -> dict:
    return {
        "type": "Results",
        "is_final": is_final,
        "speech_final": speech_final,
        "channel": {"alternatives": [{"transcript": text, "words": [{"end": 1.0}]}]},
    }


def test_deepgram_emits_interims_then_one_final(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = FakeSocket()

    async def fake_connect(*args, **kwargs):
        return sock

    monkeypatch.setattr(deepgram, "connect", fake_connect)

    async def run() -> list:
        stt = deepgram.DeepgramSpeechToText("k", model="nova-3", language="en")
        await stt.start()
        await stt.send_audio(b"\xff" * 160)
        sock.push(_dg_result("I'd like", is_final=False))
        sock.push(_dg_result("I'd like to book", is_final=True))
        sock.push(_dg_result("a table", is_final=True, speech_final=True))
        sock.push({"type": "SpeechStarted"})
        await sock.close()
        events = [event async for event in stt.events()]
        await stt.close()
        return events

    events = asyncio.run(run())
    assert [(e.kind, e.text) for e in events] == [
        ("interim", "I'd like"),
        ("interim", "I'd like to book"),
        ("final", "I'd like to book a table"),
        ("speech_started", ""),
    ]
    assert events[2].speech_ended_at is not None
    assert sock.sent[0] == b"\xff" * 160


def test_deepgram_close_stops_keepalive_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = FakeSocket()

    async def fake_connect(*args, **kwargs):
        return sock

    monkeypatch.setattr(deepgram, "connect", fake_connect)

    async def run() -> tuple:
        stt = deepgram.DeepgramSpeechToText("k", model="nova-3", language="en")
        await stt.start()
        await stt.close()
        await stt.close()  # idempotent
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return leftover, stt

    leftover, stt = asyncio.run(run())
    assert leftover == []
    assert sock.closed
    assert json.loads(sock.sent[-1]) == {"type": "CloseStream"}
    assert stt.closed


# --- Cartesia ------------------------------------------------------------------


def _cartesia_echo(sock: FakeSocket):
    """Answer each text chunk with one audio chunk; finish when ``continue`` is false."""

    def on_send(raw: str) -> None:
        message = json.loads(raw)
        if "cancel" in message:
            return
        ctx = message["context_id"]
        if message["transcript"]:
            audio = base64.b64encode(message["transcript"].encode()).decode()
            sock.push({"type": "chunk", "context_id": ctx, "data": audio})
        if not message["continue"]:
            sock.push({"type": "done", "context_id": ctx})

    return on_send


async def _texts(*chunks: str):
    for chunk in chunks:
        await asyncio.sleep(0)
        yield chunk


def test_cartesia_streams_audio_per_text_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = FakeSocket()
    sock.on_send = _cartesia_echo(sock)

    async def fake_connect(*args, **kwargs):
        return sock

    monkeypatch.setattr(cartesia, "connect", fake_connect)

    async def run() -> tuple:
        tts = cartesia.CartesiaTextToSpeech(
            "k", version="v", model_id="sonic-2", voice_id="voice", language="en"
        )
        await tts.start()
        audio = [chunk async for chunk in tts.synthesize(_texts("Hello, ", "world."))]
        await tts.close()
        leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return audio, leftover

    audio, leftover = asyncio.run(run())
    assert audio == [b"Hello, ", b"world."]
    requests = [json.loads(m) for m in sock.sent]
    assert [r["continue"] for r in requests] == [True, True, False]
    assert requests[0]["output_format"]["encoding"] == "pcm_mulaw"
    assert sock.closed
    assert leftover == []


def test_cartesia_cancels_the_context_when_the_consumer_stops_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = FakeSocket()
    sock.on_send = _cartesia_echo(sock)

    async def fake_connect(*args, **kwargs):
        return sock

    monkeypatch.setattr(cartesia, "connect", fake_connect)

    async def run() -> None:
        tts = cartesia.CartesiaTextToSpeech(
            "k", version="v", model_id="sonic-2", voice_id="voice", language="en"
        )
        await tts.start()
        stream = tts.synthesize(_texts("one, ", "two, ", "three."))
        await stream.__anext__()
        await stream.aclose()
        await tts.close()

    asyncio.run(run())
    assert any(json.loads(m).get("cancel") for m in sock.sent)


# --- OpenAI --------------------------------------------------------------------


class FakeStream:
    def __init__(self, chunks: list) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self) -> None:
        self.closed = True


def _chunk(content=None, tool_calls=None, usage=None):
    choices = (
        []
        if content is None and tool_calls is None
        else [SimpleNamespace(delta=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )
    return SimpleNamespace(choices=choices, usage=usage)


def _fake_client(stream: FakeStream, seen: dict):
    async def create(**kwargs):
        seen.update(kwargs)
        return stream

    async def close():
        seen["client_closed"] = True

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=close
    )


def test_openai_streams_text_tool_calls_and_usage() -> None:
    fragment = SimpleNamespace(
        index=0, id="call_1", function=SimpleNamespace(name="noop", arguments="{}")
    )
    stream = FakeStream(
        [
            _chunk("Hello"),
            _chunk(" there"),
            _chunk(tool_calls=[fragment]),
            _chunk(usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2)),
        ]
    )
    seen: dict = {}
    model = OpenAIChatModel(_fake_client(stream, seen), "gpt-4.1")

    async def run() -> list:
        return [event async for event in model.stream([{"role": "user", "content": "hi"}], [])]

    events = asyncio.run(run())
    assert events == [
        TextDelta("Hello"),
        TextDelta(" there"),
        UsageReport(10, 2),
        ToolCallRequest("call_1", "noop", "{}"),
    ]
    assert seen["stream"] is True and seen["model"] == "gpt-4.1"
    assert "tools" not in seen  # no tools requested in Sprint 1
    assert stream.closed


def test_openai_stream_is_closed_when_the_consumer_stops_early() -> None:
    stream = FakeStream([_chunk("a"), _chunk("b"), _chunk("c")])
    seen: dict = {}
    model = OpenAIChatModel(_fake_client(stream, seen), "gpt-4.1")

    async def run() -> None:
        events = model.stream([], [])
        await events.__anext__()
        await events.aclose()
        await model.close()

    asyncio.run(run())
    assert stream.closed
    assert seen["client_closed"]


# --- factory -------------------------------------------------------------------


def test_factory_uses_mocks_without_keys() -> None:
    vendors = build_vendors(VoiceVendorConfig(), mock_script=["hello"])
    assert isinstance(vendors.stt, ScriptedSpeechToText)
    assert isinstance(vendors.llm, MockLanguageModel)
    assert isinstance(vendors.tts, MockTextToSpeech)


def test_factory_ignores_placeholder_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DEEPGRAM_API_KEY", "OPENAI_API_KEY", "CARTESIA_API_KEY"):
        monkeypatch.setenv(name, f"your-{name.lower()}-here")
    config = VoiceVendorConfig.from_env()
    assert (config.deepgram_api_key, config.openai_api_key, config.cartesia_api_key) == ("", "", "")


def test_factory_picks_live_adapters_per_stage() -> None:
    config = VoiceVendorConfig(
        deepgram_api_key="dg", openai_api_key="oa", cartesia_api_key="ca", tts_voice_id="v1"
    )
    vendors = build_vendors(config)
    assert isinstance(vendors.stt, deepgram.DeepgramSpeechToText)
    assert isinstance(vendors.llm, OpenAIChatModel)
    assert isinstance(vendors.tts, cartesia.CartesiaTextToSpeech)
    asyncio.run(vendors.llm.close())
