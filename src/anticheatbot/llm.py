from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


async def chat_completion_json(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.2,
    timeout: float = 60.0,
) -> dict[str, Any]:
    if not api_key:
        raise LLMError("OPENAI_API_KEY is not set")
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=payload)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.warning("llm http error: %s %s", e.response.status_code, e.response.text[:500])
            raise LLMError(str(e)) from e
        data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as e:
        raise LLMError("bad llm response shape") from e
    return json.loads(content)


async def translate_markdown(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    text: str,
    source_locale: str,
    target_locale: str,
    timeout: float = 60.0,
) -> str:
    """Return translated markdown/plain text."""
    messages = [
        {
            "role": "system",
            "content": (
                "You translate UI/legal-ish group rules text. Return JSON with a single key "
                '"translated" containing the translation only. Preserve Markdown structure; '
                "do not add commentary; do not invent new rules."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "source_locale": source_locale,
                    "target_locale": target_locale,
                    "text": text,
                },
                ensure_ascii=False,
            ),
        },
    ]
    out = await chat_completion_json(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        temperature=0.1,
        timeout=timeout,
    )
    translated = out.get("translated")
    if not isinstance(translated, str):
        raise LLMError("missing translated string")
    return translated


async def moderate_message(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    text: str,
    timeout: float = 45.0,
) -> dict[str, Any]:
    user_content = text[:8000]
    log.debug(
        "moderate_message_request model=%s user_chars=%s endpoint=%s",
        model,
        len(user_content),
        base_url.rstrip("/") + "/chat/completions",
    )
    messages = [
        {
            "role": "system",
            "content": (
                'Return JSON: {"label":"ok|spam|abuse|ads","confidence":0..1,"reason":"short"} '
                "for Telegram group moderation."
            ),
        },
        {"role": "user", "content": user_content},
    ]
    out = await chat_completion_json(
        base_url=base_url,
        api_key=api_key,
        model=model,
        messages=messages,
        temperature=0.0,
        timeout=timeout,
    )
    log.debug("moderate_message_response keys=%s", list(out.keys()) if isinstance(out, dict) else type(out))
    return out
