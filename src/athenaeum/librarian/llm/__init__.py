"""LLM provider protocol, config, response types, and adapter factory.

Contract: plan section 3.4. The librarian agent loop depends only on the
``LLMProvider`` protocol; provider adapters are thin httpx-based REST
mappings (one per provider) created via ``create_provider``.

Canonical message format (provider-agnostic, translated by each adapter):

- ``{"role": "system", "content": str}``
- ``{"role": "user", "content": str}``
- ``{"role": "assistant", "content": str | None, "tool_calls": [...]}``
  where each tool call is ``{"id": str, "name": str, "arguments": dict}``
- ``{"role": "tool", "tool_call_id": str, "name": str, "content": str}``

Canonical tool schema format: ``{"name", "description", "parameters"}``
where ``parameters`` is a JSON Schema object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class LLMProviderError(Exception):
    """The provider endpoint returned an error payload or an unusable response."""


def http_status_error(provider: str, exc: httpx.HTTPStatusError) -> LLMProviderError:
    """Adapter contract: HTTP status failures surface as LLMProviderError (A16)."""
    body = exc.response.text[:200]
    return LLMProviderError(f"{provider} returned HTTP {exc.response.status_code}: {body}")


class MalformedToolArgumentsError(ValueError):
    """Tool-call arguments were not valid JSON; a model-recoverable tool error."""


class MalformedToolArguments(str):
    """str subclass holding tool-call arguments a provider emitted as invalid JSON.

    Carried on ``ToolCall.arguments`` so the parse failure surfaces at dispatch
    time, where the agent loop's existing ``except`` turns it into a
    model-recoverable tool error message, instead of raising out of
    ``provider.complete`` and killing the run (L5). Dispatch reads arguments
    via ``.get``/``[]`` — both raise ``MalformedToolArgumentsError`` before any
    backend call, and the always-true bool keeps ``args or {}`` from silently
    substituting empty defaults. Being a str keeps every serialization path
    (trace summaries, ``asdict``, ``json.dumps``) safe; adapters echo the raw
    payload verbatim when converting message history.
    """

    def __new__(cls, raw: str, error: str = "") -> MalformedToolArguments:
        obj = super().__new__(cls, raw)
        obj.error = error
        return obj

    def _deny(self, *args: Any, **kwargs: Any) -> Any:
        raise MalformedToolArgumentsError(f"malformed tool-call arguments: {self.error}")

    get = _deny

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            self._deny()
        return super().__getitem__(key)

    def __bool__(self) -> bool:
        return True


@dataclass
class LLMConfig:
    """Per-user LLM connection settings (from the librarian_configs DB row)."""

    provider: str  # 'openai' | 'anthropic' | 'gemini' | 'openrouter' | 'openai-compatible'
    model: str
    api_key: str
    base_url: str | None = None
    max_iterations: int = 10
    temperature: float | None = None
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        # Canonical missing-key representation is the empty string (CS-15);
        # some callers pass None — never let it reach an Authorization header.
        if self.api_key is None:
            self.api_key = ""


@dataclass
class ToolCall:
    """A single tool invocation requested by the model.

    ``arguments`` is normally a dict; OpenAI-family adapters put a
    ``MalformedToolArguments`` placeholder there when the model emitted
    invalid JSON (L5) — dispatch then raises ``MalformedToolArgumentsError``,
    which the agent loop feeds back to the model as a recoverable tool error.
    """

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    """Provider-normalized completion: either final text or tool calls."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] | None = None  # prompt/completion/total tokens, when reported

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class LLMProvider(Protocol):
    """One tool-calling completion method; no streaming (plan section 3.4)."""

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict],
        config: LLMConfig,
    ) -> LLMResponse: ...


def create_provider(config: LLMConfig) -> LLMProvider:
    """Build the adapter for ``config.provider``."""
    from athenaeum.librarian.llm.anthropic import AnthropicProvider
    from athenaeum.librarian.llm.gemini import GeminiProvider
    from athenaeum.librarian.llm.openai import OpenAIProvider

    adapters: dict[str, type[LLMProvider]] = {
        "openai": OpenAIProvider,
        "anthropic": AnthropicProvider,
        "gemini": GeminiProvider,
        "openrouter": OpenAIProvider,
        "openai-compatible": OpenAIProvider,
    }
    if config.provider == "openai-compatible" and not config.base_url:
        raise ValueError("Provider 'openai-compatible' requires a base_url")
    try:
        return adapters[config.provider]()
    except KeyError:
        raise ValueError(
            f"Unknown LLM provider {config.provider!r}; expected one of {sorted(adapters)}"
        ) from None
