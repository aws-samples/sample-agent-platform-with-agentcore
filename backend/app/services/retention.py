"""When a DynamoDB item may be deleted.

The platform table has DynamoDB TTL enabled on the ``ttl`` attribute (epoch
seconds). DynamoDB deletes an item some time after that moment, typically
within a few days, never before it. An item without ``ttl`` is kept forever.

``ttl`` is deliberately separate from ``expires_at``: ``expires_at`` says when
a credential stops being *valid* (checked in code on every use, refreshed on
rotation); ``ttl`` only says when the row is no longer needed at all.

Retention per record type is configured in :mod:`app.config`
(``retention_*_days``; ``0`` keeps the record forever).
"""

import time

TTL_ATTR = "ttl"

# A credential row is kept a day past its expiry: reads already reject it
# once expired, and the margin absorbs clock skew and in-flight refreshes.
CREDENTIAL_GRACE_S = 24 * 3600


def ttl_after_days(days: int, *, start: float | None = None) -> int | None:
    """Epoch seconds ``days`` from ``start`` (default now); None keeps forever."""
    if not days or days <= 0:
        return None
    return int((start if start is not None else time.time()) + days * 86400)


def ttl_after_expiry(expires_at: float) -> int:
    """For a row whose content is useless once ``expires_at`` has passed."""
    return int(expires_at) + CREDENTIAL_GRACE_S


def with_ttl(item: dict, ttl: int | None) -> dict:
    """Return ``item`` with the TTL attribute set, or unchanged when ``ttl`` is None."""
    if ttl is not None:
        item[TTL_ATTR] = ttl
    return item
