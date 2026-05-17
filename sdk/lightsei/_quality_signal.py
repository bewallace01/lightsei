"""Workspace quality-signal fetcher.

Calls GET /workspaces/me/agents/{name}/quality with the configured
api_key. Used by auto-tuners (Phase 12D.3) to check an agent's recent
verdict mix before proposing a model downgrade or interval bump —
and by bots that want to self-report ("my last 7d had 2 bads, here's
the reasons").

Fails *closed*: returns None on any error rather than an empty dict.
Unlike cost insights — where "no insights" is a legitimate state and
the caller treats `[]` the same as failure — quality has a real
"no evals yet" state (verdict_counts={0,0,0}, total_evaluations=0)
that's meaningfully different from "couldn't fetch." Returning None
on failure forces the caller to handle the distinction explicitly,
which matters for 12D.3: never auto-tune blindly when the quality
signal is unavailable.
"""
import logging
from typing import Any, Optional
from urllib.parse import quote

logger = logging.getLogger("lightsei.quality_signal")


def get_quality_signal(
    client, agent_name: str, *, days: int = 7,
) -> Optional[dict[str, Any]]:
    """Fetch this workspace's quality summary for one agent.

    Returns the dict the dashboard's /agents/{name} Quality section
    renders — `agent_name`, `days`, `verdict_counts`, `total_evaluations`,
    `recent_bads`, `trend`. Returns None on any error (unreachable
    backend, non-200, malformed body, SDK not initialized) so the
    caller can distinguish "no signal" from "no evals yet."
    """
    if not client.is_initialized() or client._http is None:
        return None

    try:
        r = client._http.get(
            f"/workspaces/me/agents/{quote(agent_name, safe='')}/quality",
            params={"days": days},
            timeout=client.timeout,
        )
    except Exception as e:
        logger.debug("get_quality_signal: backend unreachable: %s", e)
        return None

    if r.status_code != 200:
        logger.debug(
            "get_quality_signal: backend returned %s: %s",
            r.status_code, r.text[:200],
        )
        return None

    try:
        body = r.json()
    except Exception as e:
        logger.debug("get_quality_signal: malformed response: %s", e)
        return None

    # Sanity check: every quality response should at least carry
    # verdict_counts. Treat a body missing that as malformed.
    if not isinstance(body, dict) or "verdict_counts" not in body:
        return None
    return body
