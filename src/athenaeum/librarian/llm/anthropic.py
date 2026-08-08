"""Anthropic messages API adapter."""

from __future__ import annotations

import json
from typing import Any

import httpx

from athenaeum.librarian.llm import (
    LLMConfig,
    LLMProviderError,
    LLMResponse,
    ToolCall,
    http_status_error,
)

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096  # the messages API requires max_tokens


def _convert(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Split canonical messages into a system prompt and Anthropic messages.

    Tool results become ``tool_result`` content blocks inside user messages;
    consecutive tool results are merged into a single user message. A user
    text message immediately following a tool-result batch merges into that
    same user message (text block last, AGENT-02: cap-exit shape) — the
    converted sequence never holds two consecutive user entries.
    """
    system_parts: list[str] = []
    out: list[dict] = []
    pending_tool_results: list[dict] = []

    def flush_tool_results() -> None:
        if pending_tool_results:
            out.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for msg in messages:
        role = msg["role"]
        if role == "system":
            system_parts.append(msg["content"])
        elif role == "user":
            if pending_tool_results:
                # Merge: one user message whose content is the tool_result
                # blocks followed by the text block.
                pending_tool_results.append({"type": "text", "text": msg["content"]})
                flush_tool_results()
            else:
                out.append({"role": "user", "content": msg["content"]})
        elif role == "assistant":
            flush_tool_results()
            blocks: list[dict[str, Any]] = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": msg["content"]})
            for tc in msg.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc.get("arguments") or {},
                    }
                )
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg["content"],
                }
            )
        else:
            raise ValueError(f"Unknown message role {role!r}")
    flush_tool_results()
    system = "\n\n".join(system_parts) if system_parts else None
    return system, out


class AnthropicProvider:
    """Thin REST mapping onto POST {base_url}/messages."""

    def __init__(self) -> None:
        # One client per provider instance (A16): pool/TLS are reused across
        # the many completions of a run instead of rebuilt per call.
        self._client = httpx.AsyncClient(timeout=120.0)

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        config: LLMConfig,
    ) -> LLMResponse:
        base_url = (config.base_url or DEFAULT_BASE_URL).rstrip("/")
        system, converted = _convert(messages)
        body: dict[str, Any] = {
            "model": config.model,
            "max_tokens": config.max_tokens or DEFAULT_MAX_TOKENS,
            "messages": converted,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters", {}),
                }
                for t in tools
            ]
        if config.temperature is not None:
            body["temperature"] = config.temperature

        response = await self._client.post(
            f"{base_url}/messages",
            headers={
                "x-api-key": config.api_key,
                "anthropic-version": ANTHROPIC_VERSION,
            },
            json=body,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise http_status_error(config.provider, exc) from exc
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            # A 200 with a non-JSON body is a provider failure, not a raw
            # JSONDecodeError escaping the F7 classification.
            raise LLMProviderError(
                f"{config.provider} returned a non-JSON response body: {response.text[:200]}"
            ) from exc

        if data.get("type") == "error":
            error = data.get("error") or {}
            raise LLMProviderError(
                f"anthropic returned an error: {error.get('message', 'unknown error')}"
            )

        content = data.get("content")
        if not content:
            # An unusable response, not an empty answer — the loop must not
            # treat it as a finished run (parity with the Gemini CS-10 raise).
            raise LLMProviderError("anthropic returned a response without content")

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block["id"],
                        name=block["name"],
                        arguments=block.get("input") or {},
                    )
                )
        text = "".join(text_parts) or None
        raw_usage = data.get("usage") or {}
        usage = None
        if raw_usage:
            prompt_tokens = raw_usage.get("input_tokens", 0)
            completion_tokens = raw_usage.get("output_tokens", 0)
            usage = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
        return LLMResponse(text=text, tool_calls=tool_calls, usage=usage)
