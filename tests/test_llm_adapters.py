"""Tests for the LLM provider adapters (plan stream-B checklist).

httpx traffic is mocked with respx; each provider is checked for request
shape (URL, headers, message/tool mapping) and both response kinds
(final text, tool_calls).
"""

import json
from dataclasses import asdict

import httpx
import pytest
import respx

from athenaeum.librarian.llm import (
    LLMConfig,
    LLMProviderError,
    MalformedToolArguments,
    MalformedToolArgumentsError,
    create_provider,
)
from athenaeum.librarian.llm.openai import OpenAIProvider, _convert_messages
from athenaeum.librarian.tools import dispatch

SIMPLE_MESSAGES = [
    {"role": "system", "content": "You are a librarian."},
    {"role": "user", "content": "Hello"},
]

TOOLS = [
    {
        "name": "list_dir",
        "description": "List a directory",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    }
]

# A conversation round-trip: assistant tool call + tool result, provider-agnostic.
TOOL_MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "q"},
    {
        "role": "assistant",
        "content": "checking",
        "tool_calls": [{"id": "c1", "name": "list_dir", "arguments": {"path": "/"}}],
    },
    {"role": "tool", "tool_call_id": "c1", "name": "list_dir", "content": "[]"},
]


def openai_config(**overrides) -> LLMConfig:
    base = {
        "provider": "openai",
        "model": "gpt-x",
        "api_key": "sk-test",
        "temperature": 0.2,
        "max_tokens": 512,
    }
    return LLMConfig(**(base | overrides))


# --- OpenAI ---------------------------------------------------------------


@respx.mock
async def test_openai_text_response_and_request_shape():
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "hi", "tool_calls": None}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
        )
    )
    config = openai_config()
    response = await create_provider(config).complete(SIMPLE_MESSAGES, TOOLS, config)

    assert response.text == "hi"
    assert not response.has_tool_calls
    assert response.usage == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "gpt-x"
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 512
    assert body["messages"] == [
        {"role": "system", "content": "You are a librarian."},
        {"role": "user", "content": "Hello"},
    ]
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "list_dir"


@respx.mock
async def test_openai_tool_calls_response():
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "list_dir",
                                        "arguments": '{"path": "/"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )
    )
    config = openai_config()
    response = await create_provider(config).complete(SIMPLE_MESSAGES, TOOLS, config)

    assert response.has_tool_calls
    assert response.tool_calls[0].id == "call_1"
    assert response.tool_calls[0].name == "list_dir"
    assert response.tool_calls[0].arguments == {"path": "/"}
    assert response.usage is None  # not reported by this response


@respx.mock
async def test_openai_base_url_and_tool_message_mapping():
    route = respx.post("http://localhost:9000/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    config = openai_config(base_url="http://localhost:9000/v1")
    response = await create_provider(config).complete(TOOL_MESSAGES, [], config)

    assert response.text == "ok"
    body = json.loads(route.calls.last.request.content)
    assert "tools" not in body
    assistant = body["messages"][2]
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path": "/"}'
    assert body["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "[]"}


# --- Anthropic ------------------------------------------------------------


def anthropic_config(**overrides) -> LLMConfig:
    base = {"provider": "anthropic", "model": "claude-x", "api_key": "ak-test"}
    return LLMConfig(**(base | overrides))


@respx.mock
async def test_anthropic_text_response_and_request_shape():
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "answer"}],
                "usage": {"input_tokens": 12, "output_tokens": 5},
            },
        )
    )
    config = anthropic_config()
    response = await create_provider(config).complete(SIMPLE_MESSAGES, TOOLS, config)

    assert response.text == "answer"
    assert not response.has_tool_calls
    # input/output renamed onto the normalized vocabulary; total derived
    assert response.usage == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}

    request = route.calls.last.request
    assert request.headers["x-api-key"] == "ak-test"
    assert request.headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.content)
    assert body["system"] == "You are a librarian."
    assert body["max_tokens"] == 4096  # API-required default
    assert body["messages"] == [{"role": "user", "content": "Hello"}]
    assert body["tools"][0]["input_schema"] == TOOLS[0]["parameters"]


@respx.mock
async def test_anthropic_tool_use_response_and_tool_result_mapping():
    route = respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": "let me check"},
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "list_dir",
                        "input": {"path": "/"},
                    },
                ]
            },
        )
    )
    config = anthropic_config()
    response = await create_provider(config).complete(TOOL_MESSAGES, TOOLS, config)

    assert response.text == "let me check"
    assert response.has_tool_calls
    assert response.tool_calls[0].id == "tu_1"
    assert response.tool_calls[0].arguments == {"path": "/"}

    body = json.loads(route.calls.last.request.content)
    assistant = body["messages"][1]
    assert assistant["content"][0] == {"type": "text", "text": "checking"}
    assert assistant["content"][1]["type"] == "tool_use"
    assert assistant["content"][1]["input"] == {"path": "/"}
    # tool result becomes a user message with a tool_result block
    tool_result_msg = body["messages"][2]
    assert tool_result_msg["role"] == "user"
    assert tool_result_msg["content"] == [
        {"type": "tool_result", "tool_use_id": "c1", "content": "[]"}
    ]


# --- Gemini ---------------------------------------------------------------


def gemini_config(**overrides) -> LLMConfig:
    base = {
        "provider": "gemini",
        "model": "gemini-x",
        "api_key": "gk-test",
        "temperature": 0.5,
        "max_tokens": 256,
    }
    return LLMConfig(**(base | overrides))


@respx.mock
async def test_gemini_text_response_and_request_shape():
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "answer"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 8,
                    "candidatesTokenCount": 3,
                    "totalTokenCount": 11,
                },
            },
        )
    )
    config = gemini_config()
    response = await create_provider(config).complete(SIMPLE_MESSAGES, TOOLS, config)

    assert response.text == "answer"
    assert not response.has_tool_calls
    assert response.usage == {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}

    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == "gk-test"
    body = json.loads(request.content)
    assert body["systemInstruction"] == {"parts": [{"text": "You are a librarian."}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "Hello"}]}]
    assert body["tools"][0]["functionDeclarations"][0]["name"] == "list_dir"
    assert body["generationConfig"] == {"temperature": 0.5, "maxOutputTokens": 256}


@respx.mock
async def test_gemini_function_call_response_and_tool_message_mapping():
    route = respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-x:generateContent"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "list_dir",
                                        "args": {"path": "/"},
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )
    )
    config = gemini_config()
    response = await create_provider(config).complete(TOOL_MESSAGES, TOOLS, config)

    assert response.has_tool_calls
    assert response.tool_calls[0].name == "list_dir"
    assert response.tool_calls[0].arguments == {"path": "/"}
    assert response.tool_calls[0].id  # synthetic id assigned

    body = json.loads(route.calls.last.request.content)
    model_msg = body["contents"][1]
    assert model_msg["role"] == "model"
    assert model_msg["parts"][0] == {"text": "checking"}
    assert model_msg["parts"][1]["functionCall"]["name"] == "list_dir"
    fn_response = body["contents"][2]
    assert fn_response["role"] == "user"
    assert fn_response["parts"][0]["functionResponse"] == {
        "name": "list_dir",
        "response": {"result": "[]"},
    }


def test_create_provider_rejects_unknown():
    config = LLMConfig(provider="bogus", model="m", api_key="k")
    try:
        create_provider(config)
    except ValueError as exc:
        assert "bogus" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


# --- OpenRouter / OpenAI-compatible aliases --------------------------------


def test_create_provider_openrouter_alias():
    config = LLMConfig(provider="openrouter", model="vendor/model-x", api_key="or-key")
    assert isinstance(create_provider(config), OpenAIProvider)


def test_create_provider_openai_compatible_alias():
    config = LLMConfig(
        provider="openai-compatible",
        model="m",
        api_key="k",
        base_url="http://localhost:9000/v1",
    )
    assert isinstance(create_provider(config), OpenAIProvider)


@respx.mock
async def test_openrouter_default_base_url():
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    config = LLMConfig(provider="openrouter", model="vendor/model-x", api_key="or-key")
    response = await create_provider(config).complete(SIMPLE_MESSAGES, [], config)

    assert response.text == "ok"
    assert route.called


@respx.mock
async def test_openrouter_base_url_override():
    route = respx.post("http://localhost:9000/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    config = LLMConfig(
        provider="openrouter",
        model="vendor/model-x",
        api_key="or-key",
        base_url="http://localhost:9000/v1",
    )
    response = await create_provider(config).complete(SIMPLE_MESSAGES, [], config)

    assert response.text == "ok"
    assert route.called


def test_openai_compatible_requires_base_url():
    config = LLMConfig(provider="openai-compatible", model="m", api_key="k")
    try:
        create_provider(config)
    except ValueError as exc:
        assert "base_url" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


# --- Error payloads on HTTP 200 --------------------------------------------


@respx.mock
async def test_openai_error_body_on_200_raises_provider_error():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"error": {"code": 429, "message": "Rate limit exceeded"}}
        )
    )
    config = LLMConfig(provider="openrouter", model="vendor/model-x", api_key="or-key")
    try:
        await create_provider(config).complete(SIMPLE_MESSAGES, [], config)
    except LLMProviderError as exc:
        assert "Rate limit exceeded" in str(exc)
        assert "429" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected LLMProviderError")


@respx.mock
async def test_openai_missing_choices_on_200_raises_provider_error():
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"id": "chatcmpl-x", "choices": []})
    )
    config = openai_config()
    try:
        await create_provider(config).complete(SIMPLE_MESSAGES, [], config)
    except LLMProviderError as exc:
        assert "without choices" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected LLMProviderError")


@respx.mock
async def test_anthropic_error_type_on_200_raises_provider_error():
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(
            200,
            json={"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
        )
    )
    config = LLMConfig(provider="anthropic", model="claude-x", api_key="ak-test")
    try:
        await create_provider(config).complete(SIMPLE_MESSAGES, [], config)
    except LLMProviderError as exc:
        assert "Overloaded" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected LLMProviderError")


@respx.mock
async def test_anthropic_http_error_maps_to_provider_error():
    """A16: raise_for_status failures surface as LLMProviderError, not raw httpx."""
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    config = anthropic_config()
    with pytest.raises(LLMProviderError, match="HTTP 500"):
        await create_provider(config).complete(SIMPLE_MESSAGES, [], config)


@respx.mock
async def test_anthropic_reuses_one_async_client_across_completions(monkeypatch):
    """A16: one shared httpx.AsyncClient per provider instance."""
    constructions = []
    real_client = httpx.AsyncClient

    class CountingClient(real_client):
        def __init__(self, *args, **kwargs):
            constructions.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", CountingClient)
    respx.post("https://api.anthropic.com/v1/messages").mock(
        return_value=httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})
    )
    config = anthropic_config()
    provider = create_provider(config)
    await provider.complete(SIMPLE_MESSAGES, [], config)
    await provider.complete(SIMPLE_MESSAGES, [], config)
    assert len(constructions) == 1


@respx.mock
async def test_gemini_blocked_prompt_raises_provider_error():
    respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gem-x:generateContent"
    ).mock(return_value=httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}}))
    config = LLMConfig(provider="gemini", model="gem-x", api_key="gk-test")
    try:
        await create_provider(config).complete(SIMPLE_MESSAGES, [], config)
    except LLMProviderError as exc:
        assert "SAFETY" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected LLMProviderError")


# --- Step 3.2: L5 / A16 / CS-10 / CS-15 ---------------------------------------


@respx.mock
async def test_openai_malformed_tool_call_arguments_become_recoverable():
    """L5: invalid argument JSON must not raise out of complete(); it becomes
    a MalformedToolArguments placeholder that dispatch turns into a
    model-recoverable tool error."""
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_bad",
                                    "type": "function",
                                    "function": {
                                        "name": "read_document",
                                        "arguments": '{"path": "/x',  # truncated JSON
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )
    )
    config = openai_config()
    response = await create_provider(config).complete(SIMPLE_MESSAGES, TOOLS, config)

    assert response.has_tool_calls  # no raise; the run continues
    arguments = response.tool_calls[0].arguments
    assert isinstance(arguments, MalformedToolArguments)

    # The agent loop's existing recoverable path: dispatch raises a clear
    # error BEFORE any backend call; the loop feeds it back as a tool message.
    with pytest.raises(MalformedToolArgumentsError, match="malformed tool-call arguments"):
        await dispatch("read_document", arguments, backend=None)
    with pytest.raises(MalformedToolArgumentsError):
        arguments.get("path")
    assert bool(arguments) is True  # `args or {}` must not swallow it

    # History conversion echoes the model's raw payload verbatim (the API
    # expects the arguments string; fabricating "{}" would lie about history).
    converted = _convert_messages(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [asdict(response.tool_calls[0])],
            }
        ]
    )
    assert converted[0]["tool_calls"][0]["function"]["arguments"] == '{"path": "/x'


@respx.mock
async def test_openai_http_error_maps_to_provider_error():
    """A16: raise_for_status failures surface as LLMProviderError, not raw httpx."""
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )
    config = openai_config()
    with pytest.raises(LLMProviderError, match="HTTP 500"):
        await create_provider(config).complete(SIMPLE_MESSAGES, [], config)


@respx.mock
async def test_openai_reuses_one_async_client_across_completions(monkeypatch):
    """A16: one shared httpx.AsyncClient per provider instance."""
    constructions = []
    real_client = httpx.AsyncClient

    class CountingClient(real_client):
        def __init__(self, *args, **kwargs):
            constructions.append(1)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", CountingClient)
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    )
    config = openai_config()
    provider = create_provider(config)
    await provider.complete(SIMPLE_MESSAGES, [], config)
    await provider.complete(SIMPLE_MESSAGES, [], config)
    assert len(constructions) == 1


@respx.mock
async def test_gemini_http_error_maps_to_provider_error():
    respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gem-x:generateContent"
    ).mock(return_value=httpx.Response(429, text="Too Many Requests"))
    config = LLMConfig(provider="gemini", model="gem-x", api_key="gk-test")
    with pytest.raises(LLMProviderError, match="HTTP 429"):
        await create_provider(config).complete(SIMPLE_MESSAGES, [], config)


@respx.mock
async def test_gemini_empty_candidates_without_block_reason_raises():
    """CS-10: empty candidates with no blockReason is an unusable response,
    not an empty 'success' the loop would treat as a finished run."""
    respx.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gem-x:generateContent"
    ).mock(return_value=httpx.Response(200, json={"candidates": []}))
    config = LLMConfig(provider="gemini", model="gem-x", api_key="gk-test")
    with pytest.raises(LLMProviderError, match="without candidates"):
        await create_provider(config).complete(SIMPLE_MESSAGES, [], config)


def test_llm_config_none_api_key_becomes_empty_string():
    """CS-15: the canonical missing-key representation is ''; 'Bearer None'
    can never be constructed."""
    config = LLMConfig(provider="openai", model="m", api_key=None)  # type: ignore[arg-type]
    assert config.api_key == ""
