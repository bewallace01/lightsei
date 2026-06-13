"""Phase 34.1: chat-first insights ("ask your business team").

The vision's headline UX: an owner types a plain-English question and the
team answers. Routes the question to the BI assistant's question-mode and
surfaces the reply.

Async by nature: the BI assistant runs on the worker, not in the request.
So `enqueue_question` drops a bi.summarize command carrying the question +
the workspace's recent activity, and `get_answer` polls for the bi.summary
event the assistant emits, matched on the command id. The dashboard posts
once, then polls until the answer lands (or the assistant crashes).

Reuses feeder.gather_recent_activity so a question is answered against the
same rollup the weekly digest summarizes.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

# Same assistant + command kind as the digest; a distinct source marks an
# owner-asked question apart from the proactive digest/alert.
ASK_AGENT = "bi"
ASK_KIND = "bi.summarize"
ASK_SOURCE = "ask"

_COMMAND_TTL = timedelta(hours=24)
_MAX_QUESTION_LEN = 500


def enqueue_question(
    session: Session, workspace_id: str, question: str, now: datetime
) -> str:
    """Enqueue a bi.summarize question for the BI assistant. Returns the
    command id the caller polls with get_answer. Does not commit.

    The question is trimmed + length-capped (defense against an oversized
    payload); callers should validate emptiness before calling.
    """
    import feeder

    q = question.strip()[:_MAX_QUESTION_LEN]
    data = feeder.gather_recent_activity(session, workspace_id, now)

    cmd_id = str(uuid.uuid4())
    payload = {"source": ASK_SOURCE, "question": q, "data": data}
    session.execute(
        text(
            """
            INSERT INTO commands (
                id, workspace_id, agent_name, kind, payload, status,
                approval_state, approved_at, created_at, expires_at,
                dispatch_chain_id, dispatch_depth
            ) VALUES (
                :id, :ws, :agent, :kind, CAST(:payload AS JSONB), 'pending',
                'auto_approved', :now, :now, :expires,
                :chain, 0
            )
            """
        ),
        {
            "id": cmd_id,
            "ws": workspace_id,
            "agent": ASK_AGENT,
            "kind": ASK_KIND,
            "payload": json.dumps(payload),
            "now": now,
            "expires": now + _COMMAND_TTL,
            "chain": cmd_id,
        },
    )
    return cmd_id


def get_answer(
    session: Session, workspace_id: str, command_id: str
) -> dict[str, Any]:
    """Poll for the answer to a previously-asked question.

    Returns one of:
      {"status": "answered", "answer": str}
      {"status": "failed", "error": str}
      {"status": "pending"}

    Matches the BI assistant's emitted events (bi.summary on success,
    bi.crash on failure) by the command id carried in their payload, scoped
    to the workspace so one tenant can't read another's answer.
    """
    summary = session.execute(
        text(
            """
            SELECT payload
              FROM events
             WHERE workspace_id = :ws
               AND kind = 'bi.summary'
               AND payload ->> 'command_id' = :cmd
             ORDER BY timestamp DESC
             LIMIT 1
            """
        ),
        {"ws": workspace_id, "cmd": command_id},
    ).mappings().first()
    if summary is not None:
        return {
            "status": "answered",
            "answer": (summary["payload"] or {}).get("summary"),
        }

    crash = session.execute(
        text(
            """
            SELECT payload
              FROM events
             WHERE workspace_id = :ws
               AND kind = 'bi.crash'
               AND payload ->> 'command_id' = :cmd
             ORDER BY timestamp DESC
             LIMIT 1
            """
        ),
        {"ws": workspace_id, "cmd": command_id},
    ).mappings().first()
    if crash is not None:
        return {
            "status": "failed",
            "error": (crash["payload"] or {}).get("error") or "the assistant could not answer",
        }

    return {"status": "pending"}


def list_recent_asks(
    session: Session, workspace_id: str, limit: int = 10
) -> list[dict[str, Any]]:
    """Recent questions + their resolved answers, newest first.

    The questions are already persisted (ask commands) and answers
    (bi.summary / bi.crash events); this stitches them so the ask box can
    show a history that survives a refresh. Resolves all answers in two
    batched queries (not one per question), keyed on command id.
    """
    from sqlalchemy import bindparam

    rows = session.execute(
        text(
            """
            SELECT id, payload ->> 'question' AS question, created_at
              FROM commands
             WHERE workspace_id = :ws
               AND agent_name = :a
               AND kind = :k
               AND payload ->> 'source' = :src
             ORDER BY created_at DESC
             LIMIT :limit
            """
        ),
        {"ws": workspace_id, "a": ASK_AGENT, "k": ASK_KIND,
         "src": ASK_SOURCE, "limit": max(1, min(limit, 50))},
    ).mappings().all()
    if not rows:
        return []

    ids = [r["id"] for r in rows]

    def _by_command(kind: str) -> dict[str, dict[str, Any]]:
        stmt = text(
            """
            SELECT DISTINCT ON (payload ->> 'command_id')
                   payload ->> 'command_id' AS cmd, payload
              FROM events
             WHERE workspace_id = :ws
               AND kind = :kind
               AND payload ->> 'command_id' IN :ids
             ORDER BY payload ->> 'command_id', timestamp DESC
            """
        ).bindparams(bindparam("ids", expanding=True))
        return {
            r["cmd"]: (r["payload"] or {})
            for r in session.execute(
                stmt, {"ws": workspace_id, "kind": kind, "ids": ids}
            ).mappings().all()
        }

    summaries = _by_command("bi.summary")
    crashes = _by_command("bi.crash")

    out: list[dict[str, Any]] = []
    for r in rows:
        cid = r["id"]
        entry: dict[str, Any] = {
            "command_id": cid,
            "question": r["question"],
            "asked_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        if cid in summaries:
            entry.update(status="answered", answer=summaries[cid].get("summary"))
        elif cid in crashes:
            entry.update(
                status="failed",
                error=crashes[cid].get("error") or "the assistant could not answer",
            )
        else:
            entry["status"] = "pending"
        out.append(entry)
    return out


def bi_deployed(session: Session, workspace_id: str) -> bool:
    """Whether the BI assistant exists for this workspace. Without it, a
    question sits pending forever — the endpoint surfaces that up front."""
    return session.execute(
        text("SELECT 1 FROM agents WHERE workspace_id = :ws AND name = :a"),
        {"ws": workspace_id, "a": ASK_AGENT},
    ).first() is not None


def get_command_question(
    session: Session, workspace_id: str, command_id: str
) -> Optional[str]:
    """The original question text for a command (for the answer view)."""
    row = session.execute(
        text(
            "SELECT payload ->> 'question' FROM commands "
            "WHERE id = :id AND workspace_id = :ws AND agent_name = :a"
        ),
        {"id": command_id, "ws": workspace_id, "a": ASK_AGENT},
    ).first()
    return row[0] if row else None
