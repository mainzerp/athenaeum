"""OpenAI chat-completions adapter (also OpenAI-compatible endpoints via base_url)."""

from __future__ import annotations

import json
from typing import Any

import httpx

from athenaeum.librarian.llm import (
    LLMConfig,
    LLMProviderError,
    LLMResponse,
    MalformedToolArguments,
    ToolCall,
    http_status_error,
)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _convert_messages(messages: list[dict]) -> list[dict]:
    """Map canonical messages onto the OpenAI chat-completions shape."""
    out: list[dict] = []
    for msg in messages:
        role = msg["role"]
        if role in ("system", "user"):
            out.append({"role": role, "content": msg["content"]})
        elif role == "assistant":
            converted: dict[str, Any] = {"role": "assistant", "content": msg.get("content")}
            if msg.get("tool_calls"):
                converted["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": _dump_arguments(tc.get("arguments")),
                        },
                    }
                    for tc in msg["tool_calls"]
                ]
            out.append(converted)
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": msg["tool_call_id"],
                    "content": msg["content"],
                }
            )
        else:
            raise ValueError(f"Unknown message role {role!r}")
    return out


def _dump_arguments(arguments: Any) -> str:
    """Serialize tool-call arguments; echo malformed JSON verbatim (L5)."""
    if isinstance(arguments, MalformedToolArguments):
        return str(arguments)
    return json.dumps(arguments or {})


def _parse_tool_call(tc: dict) -> ToolCall:
    """One API tool call onto the canonical shape.

    Invalid argument JSON (e.g. truncated by max_tokens) does NOT raise here —
    that would kill the run from inside the adapter (L5). The raw payload rides
    along as a MalformedToolArguments placeholder so dispatch raises a
    model-recoverable tool error instead.
    """
    raw_arguments = tc["function"].get("arguments") or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        arguments = MalformedToolArguments(raw_arguments, error=str(exc))
    return ToolCall(id=tc["id"], name=tc["function"]["name"], arguments=arguments)


class OpenAIProvider:
    """Thin REST mapping onto POST {base_url}/chat/completions."""

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
        default = OPENROUTER_BASE_URL if config.provider == "openrouter" else DEFAULT_BASE_URL
        base_url = (config.base_url or default).rstrip("/")
        body: dict[str, Any] = {
            "model": config.model,
            "messages": _convert_messages(messages),
        }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {}),
                    },
                }
                for t in tools
            ]
        if config.temperature is not None:
            body["temperature"] = config.temperature
        if config.max_tokens is not None:
            body["max_tokens"] = config.max_tokens

        response = await self._client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {config.api_key}"},
            json=body,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise http_status_error(config.provider, exc) from exc
        data = response.json()

        # OpenRouter and some compatible gateways return HTTP 200 with an error
        # payload, or a success body without choices — never index blindly.
        if "error" in data:
            error = data["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            code = error.get("code") if isinstance(error, dict) else None
            raise LLMProviderError(f"{config.provider} returned an error (code={code}): {message}")
        choices = data.get("choices") or []
        if not choices:
            raise LLMProviderError(f"{config.provider} returned a response without choices")
        message = choices[0].get("message") or {}
        tool_calls = [_parse_tool_call(tc) for tc in message.get("tool_calls") or []]
        return LLMResponse(
            text=message.get("content"), tool_calls=tool_calls, usage=data.get("usage")
        )
