"""DynamoDB helpers shared by the services.

``query_all`` exists because a single ``Table.query`` returns at most 1 MB of
items and signals the rest through ``LastEvaluatedKey``. Every listing in this
backend that drives a decision (does this agent name exist, which registry
entries exist, which schedules are enabled) must see the whole partition:
system prompts alone are up to 20 KB, so a few dozen published agents push the
AGENT partition past one page, and a name that fell off the first page was
treated as free to publish (the ownership check never ran). Listings that
intentionally show "the most recent N" keep calling ``query`` with ``Limit``.
"""

from __future__ import annotations

from typing import Any


def query_all(table: Any, **kwargs: Any) -> list[dict]:
    """Every item of a query, following ``LastEvaluatedKey`` to the end.

    ``kwargs`` are passed to ``Table.query`` unchanged; a caller-supplied
    ``Limit`` is respected as a page size only, never as a total. A failing
    page raises: the caller must not act on a partial list as if it were
    complete.
    """
    items: list[dict] = []
    start_key = None
    while True:
        page_kwargs = dict(kwargs)
        if start_key is not None:
            page_kwargs["ExclusiveStartKey"] = start_key
        resp = table.query(**page_kwargs)
        items.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            return items
