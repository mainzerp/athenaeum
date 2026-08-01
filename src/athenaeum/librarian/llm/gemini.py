"""Google Gemini generateContent adapter."""

from __future__ import annotations

from typing import Any

import httpx

from athenaeum.librarian.llm import (
    LLMConfig,
    LLMProviderError,
    LLMResponse,
    ToolCall,
    http_status_error,
)

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def _convert(messages: list[dict]) -> tuple[dict | None, list[dict]]:
    """Split canonical messages into a systemInstruction and Gemini contents.

    Gemini function calls carry no IDs; tool results are matched by function
    name. Synthetic IDs are assigned to parsed tool calls on the way out.
    """
    system_parts: list[str] = []
    contents: list[dict] = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            system_parts.append(msg["content"])
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": msg["content"]}]})
        elif role == "assistant":
            parts: list[dict[str, Any]] = []
            if msg.get("content"):
                parts.append({"text": msg["content"]})
            for tc in msg.get("tool_calls") or []:
                parts.append(
                    {
                        "functionCall": {
                            "name": tc["name"],
                            "args": tc.get("arguments") or {},
                        }
                    }
                )
            contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": msg["name"],
                                "response": {"result": msg["content"]},
                            }
                        }
                    ],
                }
            )
        else:
            raise ValueError(f"Unknown message role {role!r}")
    system = {"parts": [{"text": "\n\n".join(system_parts)}]} if system_parts else None
    return system, contents


class GeminiProvider:
    """Thin REST mapping onto POST {base_url}/models/{model}:generateContent."""

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
        system, contents = _convert(messages)
        body: dict[str, Any] = {"contents": contents}
        if system:
            body["systemInstruction"] = system
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t["name"],
                            "description": t.get("description", ""),
                            "parameters": t.get("parameters", {}),
                        }
                        for t in tools
                    ]
                }
            ]
        generation_config: dict[str, Any] = {}
        if config.temperature is not None:
            generation_config["temperature"] = config.temperature
        if config.max_tokens is not None:
            generation_config["maxOutputTokens"] = config.max_tokens
        if generation_config:
            body["generationConfig"] = generation_config

        response = await self._client.post(
            f"{base_url}/models/{config.model}:generateContent",
            headers={"x-goog-api-key": config.api_key},
            json=body,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise http_status_error(config.provider, exc) from exc
        data = response.json()

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            if reason:
                raise LLMProviderError(f"gemini blocked the prompt: {reason}")
            # No candidates and no block reason: an unusable response, not an
            # empty answer — the loop must not treat it as a finished run (CS-10).
            raise LLMProviderError(f"{config.provider} returned a response without candidates")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        for index, part in enumerate(parts):
            if "text" in part:
                text_parts.append(part["text"])
            elif "functionCall" in part:
                call = part["functionCall"]
                tool_calls.append(
                    ToolCall(
                        id=f"gemini-{index}",
                        name=call["name"],
                        arguments=call.get("args") or {},
                    )
                )
        text = "".join(text_parts) or None
        raw_usage = data.get("usageMetadata") or {}
        usage = None
        if raw_usage:
            usage = {
                "prompt_tokens": raw_usage.get("promptTokenCount", 0),
                "completion_tokens": raw_usage.get("candidatesTokenCount", 0),
                "total_tokens": raw_usage.get("totalTokenCount", 0),
            }
        return LLMResponse(text=text, tool_calls=tool_calls, usage=usage)
