from __future__ import annotations

import json
from typing import Any

from aiohttp import web

# Below aiohttp Application default ``client_max_size`` (1 MiB); fail fast on huge bodies.
_MAX_JSON_BYTES = 512 * 1024


async def json_object(request: web.Request) -> dict[str, Any]:
    cl = request.content_length
    if cl is not None and cl > _MAX_JSON_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=_MAX_JSON_BYTES, actual_size=cl)
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise web.HTTPBadRequest(text="invalid json") from e
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="expected json object")
    return body
