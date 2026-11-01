import asyncio
import json

import httpx
import pytest

from anticheatbot.llm import translate_markdown


@pytest.mark.parametrize("model", ["gpt-6-luna", "openai/gpt-6-luna", "gpt-5.4-mini"])
def test_translation_uses_supported_temperature(monkeypatch, model):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"translated":"Hello"}'}}]})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs))
    translated = asyncio.run(translate_markdown(
        base_url="https://example.test", api_key="test", model=model,
        text="你好", source_locale="zh-Hans", target_locale="en",
    ))
    assert translated == "Hello"
    assert requests[0]["model"] == model
    assert requests[0]["response_format"] == {"type": "json_object"}
    if model.endswith("gpt-6-luna"):
        assert "temperature" not in requests[0]
    else:
        assert requests[0]["temperature"] == 0.1
