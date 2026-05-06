"""Workspace cost-insights fetcher.

Calls GET /workspaces/me/cost/insights with the configured api_key. Used
by cron-style bots (Polaris) to narrate spending in their plan stream
rather than forcing the user to a dedicated dashboard page.

Fails *open*: a network error or non-200 returns an empty list. Cost
insights are enrichment, not essential — a flapping backend should not
block Polaris's tick. Hard rule #4 territory.
"""
import logging
from typing import Any

logger = logging.getLogger("lightsei.cost_insights")


def get_cost_insights(client) -> list[dict[str, Any]]:
    """Fetch the homogeneous list of cost insights for this workspace.

    Each insight is a dict with `kind`, `headline`, `detail`, `apply`
    (the same shape `/cost/insights` renders). Returns [] on any error
    so the caller can `if insights:` without try/except plumbing.
    """
    if not client.is_initialized() or client._http is None:
        return []

    try:
        r = client._http.get(
            "/workspaces/me/cost/insights",
            timeout=client.timeout,
        )
    except Exception as e:
        logger.debug("get_cost_insights: backend unreachable: %s", e)
        return []

    if r.status_code != 200:
        logger.debug(
            "get_cost_insights: backend returned %s: %s",
            r.status_code, r.text[:200],
        )
        return []

    try:
        body = r.json()
    except Exception as e:
        logger.debug("get_cost_insights: malformed response: %s", e)
        return []

    items = body.get("insights")
    if not isinstance(items, list):
        return []
    return items
