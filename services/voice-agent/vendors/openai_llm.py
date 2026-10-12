"""Streaming chat completions with tool calls (the agent that talks)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from vendors.base import LlmEvent, TextDelta, ToolCallRequest, UsageReport


class OpenAIChatModel:
    def __init__(
        self,
        client: AsyncOpenAI,
        model: str,
        *,
        temperature: float = 0.3,
        max_tokens: int = 250,
    ) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        self.closed = False

    async def stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[LlmEvent]:
        extra: dict[str, Any] = {}
        if tools:
            extra = {"tools": tools, "tool_choice": "auto", "parallel_tool_calls": True}
        stream = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
            temperature=self._temperature,
            max_completion_tokens=self._max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            **extra,
        )

        # Tool call arguments arrive in fragments keyed by index.
        calls: dict[int, dict[str, str]] = {}
        try:
            async for chunk in stream:
                if chunk.usage is not None:
                    yield UsageReport(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    yield TextDelta(delta.content)
                for fragment in delta.tool_calls or []:
                    slot = calls.setdefault(fragment.index, {"id": "", "name": "", "arguments": ""})
                    if fragment.id:
                        slot["id"] = fragment.id
                    if fragment.function is not None:
                        if fragment.function.name:
                            slot["name"] = fragment.function.name
                        if fragment.function.arguments:
                            slot["arguments"] += fragment.function.arguments
        finally:
            await stream.close()

        for index in sorted(calls):
            slot = calls[index]
            yield ToolCallRequest(slot["id"] or f"call_{index}", slot["name"], slot["arguments"])

    async def close(self) -> None:
        self.closed = True
        await self._client.close()
