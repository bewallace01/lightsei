"""Phase 21.9: Polaris widget-incident-response.

Scans a workspace's open `widget_escalations` for patterns,
generates a `suggested_fix` for each cluster (via Anthropic), and
either persists it on the escalation rows for operator review OR
auto-applies it to the customer-facing bot's `system_prompt` when
the workspace has opted in (`polaris_auto_apply_widget_fixes`).

Pure module — no FastAPI routing in here. The endpoint glue lives
in `main.py` as `POST /workspaces/me/widget-incident-response/scan`.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session


logger = logging.getLogger("lightsei.widget_incident_response")


# Defaults — operators can override at scan call time but these
# are the demo-tuned values.
DEFAULT_LOOKBACK_HOURS = 24
DEFAULT_MIN_CLUSTER_SIZE = 3
# Cap how much sample text we send to Anthropic per cluster so a
# noisy thread doesn't blow up the prompt.
SAMPLE_TEXT_CHAR_CAP = 600
# Number of sample messages per cluster passed to the LLM. Picks
# the freshest N from each cluster.
SAMPLE_MESSAGES_PER_CLUSTER = 5


# ---------- Clustering ---------- #


# Simple stopwords list for the keyword-overlap clusterer. Doesn't
# need to be exhaustive — the v1 demo bar is "group similar refund
# questions together", not "perfect natural-language clustering."
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "it", "to", "for", "of", "and", "or",
    "in", "on", "at", "be", "are", "was", "were", "i", "you", "we",
    "they", "this", "that", "with", "do", "does", "did", "have",
    "has", "had", "my", "your", "our", "their", "me", "us", "them",
    "but", "so", "if", "as", "can", "could", "would", "should",
    "will", "would", "what", "when", "where", "how", "why", "who",
    "from", "not", "no", "yes", "just", "any", "all", "some",
    "thanks", "thank", "please", "hi", "hello", "hey", "ok", "okay",
})


def _tokenize(text: str) -> list[str]:
    """Lowercase + strip non-word chars + drop stopwords + short
    tokens. Returns the kept tokens in insertion order."""
    if not text:
        return []
    words = re.findall(r"[a-z']{3,}", text.lower())
    return [w for w in words if w not in _STOPWORDS]


def _cluster_signature(messages: list[str]) -> set[str]:
    """Pick the top keywords across the cluster as a signature.
    Top-5 by frequency is the v1 heuristic — enough to drive the
    cluster prompt + show the operator what the cluster is about."""
    counter: Counter[str] = Counter()
    for text in messages:
        counter.update(_tokenize(text))
    return {tok for tok, _ in counter.most_common(5)}


def _looks_similar(a_tokens: set[str], b_tokens: set[str]) -> bool:
    """Two messages cluster together if they share at least 2
    non-stopword tokens. Cheap; tuned for the demo where escalation
    reasons funnel similar phrasing ("refund", "cancel", "billing")."""
    if not a_tokens or not b_tokens:
        return False
    return len(a_tokens & b_tokens) >= 2


def find_escalation_clusters(
    session: Session,
    workspace_id: str,
    *,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    min_size: int = DEFAULT_MIN_CLUSTER_SIZE,
) -> list[dict[str, Any]]:
    """Return clusters of open `widget_escalations` for a workspace.

    Clustering algorithm (v1, intentionally simple):

      1. Pull open escalations from the last `lookback_hours`.
      2. Group by `reason` keyword first.
      3. Within each reason group, do greedy single-link clustering
         on the *last user message* token overlap.
      4. Drop clusters smaller than `min_size`.

    Returns one dict per cluster: `{reason, escalation_ids,
    sample_messages, keywords}`. `sample_messages` is the freshest
    `SAMPLE_MESSAGES_PER_CLUSTER` user messages truncated to
    `SAMPLE_TEXT_CHAR_CAP` chars each — bounded for the LLM prompt.
    """
    from models import WidgetConversation, WidgetEscalation, WidgetMessage

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=lookback_hours)

    # Pull open escalations + their conversation rows in a tenant-
    # scoped join.
    rows = session.execute(
        select(WidgetEscalation, WidgetConversation)
        .join(
            WidgetConversation,
            WidgetEscalation.conversation_id == WidgetConversation.id,
        )
        .where(
            WidgetConversation.workspace_id == workspace_id,
            WidgetEscalation.resolved_at.is_(None),
            WidgetEscalation.escalated_at >= since,
        )
        .order_by(WidgetEscalation.escalated_at.desc())
    ).all()

    if not rows:
        return []

    # For each escalation pull its most-recent user message (the one
    # that triggered the handoff). Bulk-fetch to dodge N+1.
    conv_ids = list({c.id for _, c in rows})
    last_user_msgs: dict[str, str] = {}
    if conv_ids:
        # Per-conversation most-recent user message via window-ish
        # trick: pull all user messages, group in Python.
        user_rows = session.execute(
            select(WidgetMessage)
            .where(
                WidgetMessage.conversation_id.in_(conv_ids),
                WidgetMessage.role == "user",
            )
            .order_by(WidgetMessage.id.desc())
        ).scalars().all()
        for m in user_rows:
            if m.conversation_id not in last_user_msgs:
                last_user_msgs[m.conversation_id] = m.text

    # Group by reason → greedy single-link cluster on tokens.
    by_reason: dict[str, list[tuple[WidgetEscalation, str]]] = defaultdict(list)
    for esc, conv in rows:
        text = last_user_msgs.get(conv.id, "")
        by_reason[esc.reason].append((esc, text))

    clusters: list[dict[str, Any]] = []
    for reason, items in by_reason.items():
        # Within a reason, group items by token overlap.
        groups: list[list[tuple[WidgetEscalation, str]]] = []
        for item in items:
            esc, text = item
            item_tokens = set(_tokenize(text))
            placed = False
            for g in groups:
                # Compare against the group's representative (first
                # item). Cheap; doesn't need full pairwise.
                _, rep_text = g[0]
                rep_tokens = set(_tokenize(rep_text))
                if _looks_similar(item_tokens, rep_tokens):
                    g.append(item)
                    placed = True
                    break
            if not placed:
                groups.append([item])

        # Reason groups with no text-overlap subgroups still cluster
        # as one (e.g. `bot_crash` doesn't have user-message-shaped
        # text). If the whole reason has fewer than min_size, drop.
        if len(items) >= min_size and len(groups) == 1:
            # No subgrouping happened (or all merged) — treat whole
            # reason as one cluster.
            clusters.append(_make_cluster(reason, items))
            continue

        for g in groups:
            if len(g) < min_size:
                continue
            clusters.append(_make_cluster(reason, g))

    return clusters


def _make_cluster(
    reason: str,
    items: list[tuple[Any, str]],
) -> dict[str, Any]:
    """Build the cluster dict the rest of the pipeline consumes."""
    escalation_ids = [esc.id for esc, _ in items]
    sample_texts = [t for _, t in items if t]
    # Truncate each sample + cap count.
    samples = [
        (t[:SAMPLE_TEXT_CHAR_CAP] + ("…" if len(t) > SAMPLE_TEXT_CHAR_CAP else ""))
        for t in sample_texts[:SAMPLE_MESSAGES_PER_CLUSTER]
    ]
    keywords = sorted(_cluster_signature(sample_texts))
    return {
        "reason": reason,
        "escalation_ids": escalation_ids,
        "sample_messages": samples,
        "keywords": keywords,
        "size": len(escalation_ids),
    }


# ---------- Suggested-fix generation ---------- #


# Prompt for the Anthropic call. Stays short and structured so the
# response is a single JSON object the caller can parse without
# tool-use ceremony.
_FIX_GENERATION_PROMPT = (
    "You are Lightsei's customer-support pattern analyzer.\n"
    "\n"
    "An operator has connected a chatbot to a customer-facing widget. "
    "The bot has escalated several conversations to a human for the "
    "same reason. Your job is to propose a single, narrow improvement "
    "to the bot's system prompt that would help it answer this kind "
    "of question itself next time.\n"
    "\n"
    "Escalation reason: {reason}\n"
    "Cluster keywords: {keywords}\n"
    "Number of escalations: {size}\n"
    "\n"
    "Sample user messages that triggered the escalations:\n"
    "{samples}\n"
    "\n"
    "Respond with ONLY a JSON object (no surrounding text) with this "
    "shape:\n"
    "{{\n"
    '  "kind": "system_prompt_addendum" | "add_faq_entry",\n'
    '  "summary": "<one sentence, <120 chars, plain prose>",\n'
    '  "detail": "<markdown, 1-3 short paragraphs, written as bot '
    "guidance>\"\n"
    "}}\n"
    "\n"
    "Pick 'system_prompt_addendum' when the bot's wording is the "
    "fix; pick 'add_faq_entry' when the bot needs a piece of "
    "knowledge it doesn't have. Default to system_prompt_addendum.\n"
    "Keep `detail` actionable: write it as a sentence the operator "
    "could paste into the bot's prompt verbatim."
)


def _build_fix_prompt(cluster: dict[str, Any]) -> str:
    samples_block = "\n".join(
        f"{i + 1}. {s}" for i, s in enumerate(cluster.get("sample_messages") or [])
    ) or "(no message text — escalations have no user-message context)"
    return _FIX_GENERATION_PROMPT.format(
        reason=cluster["reason"],
        keywords=", ".join(cluster.get("keywords") or []) or "(none)",
        size=cluster["size"],
        samples=samples_block,
    )


def generate_suggested_fix(
    cluster: dict[str, Any],
    anthropic_key: str,
    *,
    anthropic_client_factory=None,
) -> Optional[dict[str, Any]]:
    """Call Anthropic to generate a suggested_fix dict for a cluster.

    Returns None on transport / parse failures — caller treats None
    as "no fix this scan; try again later." The 21.9 scan endpoint
    swallows None silently so a flaky Anthropic call doesn't take
    down the whole scan.

    `anthropic_client_factory` is the test injection point — tests
    pass a fake that returns a canned dict.
    """
    if anthropic_client_factory is not None:
        client = anthropic_client_factory(anthropic_key)
    else:
        import anthropic
        client = anthropic.Anthropic(api_key=anthropic_key, max_retries=2)

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=600,
            messages=[
                {"role": "user", "content": _build_fix_prompt(cluster)},
            ],
        )
    except Exception as exc:
        logger.warning(
            "widget_incident_response: Anthropic call failed for "
            "cluster reason=%s size=%d: %s",
            cluster["reason"], cluster["size"], exc,
        )
        return None

    # Pull the text content out of the response. Anthropic returns a
    # list of content blocks; we want the concatenated text.
    text_parts = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            text_parts.append(block.get("text") or "")
    raw = "".join(text_parts).strip()
    if not raw:
        return None

    # The prompt asks for JSON-only but defensive-strip ```json
    # fences in case the model wraps it anyway.
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)

    try:
        parsed = json.loads(raw)
    except Exception as exc:
        logger.warning(
            "widget_incident_response: failed to parse fix JSON: %s; "
            "raw=%r", exc, raw[:200],
        )
        return None

    # Validate the parsed shape; bad responses are no-ops.
    kind = parsed.get("kind")
    detail = parsed.get("detail")
    summary = parsed.get("summary") or ""
    if kind not in ("system_prompt_addendum", "add_faq_entry"):
        return None
    if not isinstance(detail, str) or not detail.strip():
        return None

    return {
        "kind": kind,
        "summary": str(summary)[:200],
        "detail": detail.strip(),
        # Stamp the cluster's metadata for the operator's context.
        "keywords": cluster.get("keywords") or [],
        "cluster_size": cluster["size"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------- Apply path ---------- #


def append_fix_to_system_prompt(
    bot_system_prompt: Optional[str],
    suggested_fix: dict[str, Any],
    *,
    applied_at: Optional[datetime] = None,
) -> str:
    """Pure function: produce the new system_prompt that results from
    applying `suggested_fix` to `bot_system_prompt`.

    Appends a marked section to the end of the existing prompt so the
    operator can find + revert it later if needed. Format:

        <existing prompt>

        # Polaris-suggested fix applied 2026-05-23T...
        <detail>
    """
    applied_at = applied_at or datetime.now(timezone.utc)
    detail = (suggested_fix or {}).get("detail") or ""
    header = (
        f"# Polaris-suggested fix applied {applied_at.isoformat()}"
    )
    base = (bot_system_prompt or "").rstrip()
    if base:
        return f"{base}\n\n{header}\n{detail}"
    return f"{header}\n{detail}"
