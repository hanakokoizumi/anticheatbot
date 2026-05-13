from __future__ import annotations

import json
from typing import Any

from aiohttp import web


async def json_object(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise web.HTTPBadRequest(text="invalid json") from e
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="expected json object")
    return body
