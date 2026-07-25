"""Request-scoped caller context.

The invocation pipeline sometimes needs the *end user's* own bearer token —
not the platform's IAM identity — for example when an attached MCP server is
an AgentCore Gateway that authenticates the caller itself and brokers that
identity to downstream APIs (see ``IDENTITY_PLACEHOLDER``).

Threading the raw token through every service signature would leak an auth
concern into unrelated call paths, so it lives in a context variable set by
:class:`CallerTokenMiddleware` and read only where identity forwarding is
requested. The token is never persisted: registry entries store the
placeholder.

The middleware must be plain ASGI rather than a FastAPI dependency: sync
dependencies and sync endpoints run in *different* threadpool workers, each
with its own copy of the context, so a value set inside a dependency is
invisible to the endpoint. ASGI middleware shares the request's task context.
"""

import contextvars

# Registry entries opt into identity forwarding by putting this placeholder in
# a header value, e.g. {"Authorization": "Bearer {{user_token}}"}.
IDENTITY_PLACEHOLDER = "{{user_token}}"  # nosec B105 - placeholder, not a secret

_caller_token: contextvars.ContextVar[str] = contextvars.ContextVar("caller_token", default="")


def set_caller_token(token: str) -> None:
    _caller_token.set(token or "")


def get_caller_token() -> str:
    """The current request's end-user bearer token ("" for internal callers
    such as the scheduler Lambda or local development)."""
    return _caller_token.get()


class CallerTokenMiddleware:
    """Capture the request's bearer token into the request context.

    The value is recorded before authentication runs, so it is only ever
    *used* by handlers that the auth dependency has already validated (an
    unauthenticated request never reaches one).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            token = ""
            for key, value in scope.get("headers") or []:
                if key == b"authorization":
                    raw = value.decode("latin-1")
                    if raw.startswith("Bearer "):
                        token = raw[len("Bearer ") :]
                    break
            set_caller_token(token)
        await self.app(scope, receive, send)
