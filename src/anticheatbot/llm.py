from __future__ import annotations

import json
import logging
import math
import unicodedata
from typing import Any

import httpx

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def has_visible_text(value: object) -> bool:
    """Keep formatting intact while rejecting empty/control/format-only translations."""
    if not isinstance(value, str):
        return False
    # These Unicode letter/symbol fillers render blank despite not being whitespace.
    blank_fillers = "\u115f\u1160\u2800\u3164\uffa0"
    return any(
        char not in blank_fillers and unicodedata.category(char)[0] in "LNPS"
        for char in value
    )


async def chat_completion_json(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    messages: list[dict[str, str]],
    temperature: float | None = 0.2,
    timeout: float = 60.0,
) -> dict[str, Any]:
    if not api_key:
        raise LLMError("OPENAI_API_KEY is not set")
    url = base_url.rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    # Luna accepts only its default temperature, for translation and moderation alike.
    if temperature is not None and model.rsplit("/", 1)[-1] != "gpt-6-luna":
        payload["temperature"] = temperature
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        # Callers rely on LLMError to fall back on connection errors and timeouts too.
        raise LLMError(f"LLM request failed: {type(e).__name__}") from e
    except ValueError as e:
        raise LLMError("LLM returned invalid JSON") from e
    try:
        content = data["choices"][0]["message"]["content"]
        result = json.loads(content)
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise LLMError("bad llm response shape") from e
    if not isinstance(result, dict):
        raise LLMError("LLM response must be a JSON object")
    return result


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
    if not has_visible_text(translated):
        raise LLMError("missing or blank translated string")
    return translated


async def moderate_message(
    *,
    base_url: str,
    api_key: str | None,
    model: str,
    text: str,
    mention_usernames: list[str] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"message_text": text[:8000]}
    if mention_usernames:
        payload["telegram_mention_usernames"] = mention_usernames[:48]
    user_content = json.dumps(payload, ensure_ascii=False)
    log.debug(
        "moderate_message_request model=%s user_chars=%s mentions=%s endpoint=%s",
        model,
        len(text[:8000]),
        len(mention_usernames or []),
        base_url.rstrip("/") + "/chat/completions",
    )
    messages = [
        {
            "role": "system",
            "content": (
                'Return JSON: {"label":"ok|spam|abuse|ads","confidence":0..1,"reason":"short"} '
                "for Telegram supergroup text moderation. "
                "Label spam/ads for junk, solicitation, scams, or engagement farming. "
                "If the message @-mentions multiple distinct usernames that end with the suffix "
                '"bot" (case-insensitive, e.g. "@foo_bot", "@Bar_Bot") and the remaining visible text '
                "has no substantive conversational value (mostly tags, filler punctuation, hype, "
                "or no real on-topic discussion), classify as spam with confidence at least 0.9 "
                "unless the message clearly contains legitimate substantive content."
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
    confidence = out.get("confidence")
    if (
        out.get("label") not in ("ok", "spam", "abuse", "ads")
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
        or not math.isfinite(confidence)
    ):
        raise LLMError("invalid moderation verdict")
    return out
