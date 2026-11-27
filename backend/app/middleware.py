"""Pure ASGI middleware adding X-Served-By and X-App-Version to every HTTP response.

It wraps the *finished* FastAPI application from the outside (see main.create_app),
so it also sees responses produced by Starlette's own error layers: 404, 405, 422,
503 from our handlers and the 500 from ServerErrorMiddleware.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

_OWN_HEADERS = (b"x-served-by", b"x-app-version")


def _encode(value: str) -> bytes:
    # Header values must be latin-1; replace anything else rather than failing.
    return value.encode("latin-1", errors="replace")


class HeaderMiddleware:
    def __init__(self, app: ASGIApp, served_by: str, app_version: str) -> None:
        self.app = app
        self._headers = [
            (b"x-served-by", _encode(served_by)),
            (b"x-app-version", _encode(app_version)),
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # lifespan (startup/shutdown) and websocket scopes pass straight through.
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in _OWN_HEADERS
                ]
                headers.extend(self._headers)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)
