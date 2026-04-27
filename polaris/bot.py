"""Polaris — project orchestrator bot.

Reads a project's MEMORY.md and TASKS.md from the bundle root on every
tick, calls Claude via a forced `submit_plan` tool call (strict schema
guarantees the structured output), and emits the result as a
`polaris.plan` event so the dashboard can render it.

Phase 6A scope: read-only. No PRs, no command dispatch. Polaris produces
visible recommendations only. See TASKS.md "Phase 6" for the demo
criterion and the 6B+ roadmap.

Phase 6.5 switched the structured-output mechanism from "ask for JSON
in the prompt and parse" to Anthropic tool use with `strict: true` and
`tool_choice` forced to `submit_plan`. The model now returns a typed
input dict directly — no JSON parser, no parse-error retry path. Also
opted into adaptive thinking with effort=high since orchestrator
planning is intelligence-sensitive (skill guidance for 4.7).

Phase 6.2 added in-process change detection: the bot remembers the
last successfully-emitted doc hashes and skips the LLM call when both
files are byte-identical. A fresh deploy resets that state, so
re-deploying always regenerates a plan even on unchanged docs.

Env (defaults in parens):
  POLARIS_POLL_S     seconds between ticks (3600)
  POLARIS_MODEL      Claude model id (claude-opus-4-7)
  POLARIS_DOCS_DIR   where to find MEMORY.md / TASKS.md (.)
  POLARIS_DRY_RUN    skip the Anthropic call, useful for verification (unset)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY    required; bot authenticates to Lightsei with this
  ANTHROPIC_API_KEY   required unless POLARIS_DRY_RUN=1
"""

import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Optional

import lightsei


POLL_S = float(os.environ.get("POLARIS_POLL_S", "3600"))
MODEL = os.environ.get("POLARIS_MODEL", "claude-opus-4-7")
DOCS_DIR = Path(os.environ.get("POLARIS_DOCS_DIR", "."))
DRY_RUN = os.environ.get("POLARIS_DRY_RUN") == "1"
SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"

# In-process change-detection state. Reset on every bot restart, so a
# redeploy always regenerates a plan against the current docs.
_last_hashes: Optional[dict] = None


def _read_docs() -> dict:
    memory = (DOCS_DIR / "MEMORY.md").read_text()
    tasks = (DOCS_DIR / "TASKS.md").read_text()
    return {
        "memory_md": memory,
        "tasks_md": tasks,
        "hashes": {
            "memory_md": hashlib.sha256(memory.encode()).hexdigest()[:16],
            "tasks_md": hashlib.sha256(tasks.encode()).hexdigest()[:16],
        },
    }


SUBMIT_PLAN_TOOL = {
    "name": "submit_plan",
    "description": (
        "Submit the orchestrator plan for the project. Call this tool exactly "
        "once with a structured plan. next_actions must contain 3 to 5 items; "
        "lead with the current NOW task and fill remaining slots with the "
        "obvious follow-ons in the active phase."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "1-2 sentences on the current project state. Lead with "
                    "the active phase number and what just shipped."
                ),
            },
            "next_actions": {
                "type": "array",
                "description": "3 to 5 next actions, ordered by priority.",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": (
                                "The action. Cite phase / task numbers and "
                                "file paths when applicable."
                            ),
                        },
                        "why": {
                            "type": "string",
                            "description": (
                                "1-2 sentences on why this is the right next "
                                "step given the current state."
                            ),
                        },
                        "blocked_by": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "description": (
                                "What blocks this action (a missing secret, "
                                "an upstream dependency, a decision), or "
                                "null if unblocked."
                            ),
                        },
                    },
                    "required": ["task", "why", "blocked_by"],
                    "additionalProperties": False,
                },
            },
            "parking_lot_promotions": {
                "type": "array",
                "description": (
                    "Parking-lot items that look ready to promote given the "
                    "current state. Empty list is fine if nothing stands out."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "item": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["item", "why"],
                    "additionalProperties": False,
                },
            },
            "drift": {
                "type": "array",
                "description": (
                    "Real contradictions between MEMORY.md, TASKS.md, and "
                    "the Done Log. Stylistic differences don't count. "
                    "Empty list is fine."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "between": {
                            "type": "string",
                            "description": (
                                "Which files / sections the contradiction is "
                                "between, e.g. 'MEMORY.md vs TASKS.md'."
                            ),
                        },
                        "observation": {"type": "string"},
                    },
                    "required": ["between", "observation"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "summary",
            "next_actions",
            "parking_lot_promotions",
            "drift",
        ],
        "additionalProperties": False,
    },
}


def _call_claude(system_prompt: str, docs: dict) -> dict:
    """Calls Claude with a forced submit_plan tool call.

    `strict: true` + `tool_choice` to a specific tool guarantees the
    response contains exactly one tool_use block whose `input` matches
    the schema. No JSON parsing, no retry-on-parse-error path.
    """
    import anthropic

    user_msg = (
        f"<MEMORY.md>\n{docs['memory_md']}\n</MEMORY.md>\n\n"
        f"<TASKS.md>\n{docs['tasks_md']}\n</TASKS.md>"
    )
    client = anthropic.Anthropic()
    # Note: adaptive thinking is incompatible with `tool_choice` forcing a
    # specific tool (Opus 4.7 returns 400). For Polaris we want the
    # guaranteed schema match more than the visible reasoning, so we drop
    # thinking and rely on effort=high. If we ever want both, switch to
    # `tool_choice: {"type": "any"}` (still forces a tool call, but allows
    # thinking) — works because `submit_plan` is the only tool defined.
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "high"},
        system=system_prompt,
        tools=[SUBMIT_PLAN_TOOL],
        tool_choice={"type": "tool", "name": "submit_plan"},
        messages=[{"role": "user", "content": user_msg}],
    )

    tool_block = next(
        (b for b in resp.content if b.type == "tool_use"
         and b.name == "submit_plan"),
        None,
    )
    if tool_block is None:
        # Forced tool_choice should make this unreachable, but guard anyway:
        # surface the stop_reason so it lands in logs / dashboard.
        raise RuntimeError(
            f"no submit_plan tool_use in response (stop_reason="
            f"{resp.stop_reason})"
        )

    return {
        "input": tool_block.input,
        "model": resp.model,
        "tokens_in": resp.usage.input_tokens,
        "tokens_out": resp.usage.output_tokens,
        "stop_reason": resp.stop_reason,
    }


@lightsei.track
def tick() -> None:
    global _last_hashes
    docs = _read_docs()
    print(
        f"docs: memory={docs['hashes']['memory_md']} "
        f"tasks={docs['hashes']['tasks_md']}",
        flush=True,
    )

    if _last_hashes == docs["hashes"]:
        print("docs unchanged since last plan, skipping LLM call", flush=True)
        lightsei.emit(
            "polaris.tick_skipped",
            {"reason": "docs unchanged", "hashes": docs["hashes"]},
        )
        return

    if DRY_RUN:
        print("dry run: skipping Anthropic call", flush=True)
        lightsei.emit("polaris.tick_dry_run", {"hashes": docs["hashes"]})
        _last_hashes = docs["hashes"]
        return

    system_prompt = SYSTEM_PROMPT_PATH.read_text()
    result = _call_claude(system_prompt, docs)
    plan = result["input"]

    payload = {
        # Pretty-print the structured input so the dashboard's "raw response"
        # expander has something readable. The structured fields below are
        # the source of truth for rendering.
        "text": json.dumps(plan, indent=2),
        "doc_hashes": docs["hashes"],
        "model": result["model"],
        "tokens_in": result["tokens_in"],
        "tokens_out": result["tokens_out"],
        "summary": plan["summary"],
        "next_actions": plan["next_actions"],
        "parking_lot_promotions": plan["parking_lot_promotions"],
        "drift": plan["drift"],
    }

    print(
        f"plan: {len(plan['next_actions'])} actions, "
        f"{len(plan['parking_lot_promotions'])} promotions, "
        f"{len(plan['drift'])} drift items "
        f"({result['tokens_in']} in / {result['tokens_out']} out)",
        flush=True,
    )

    lightsei.emit("polaris.plan", payload)
    _last_hashes = docs["hashes"]


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "polaris")

    if not api_key:
        print("LIGHTSEI_API_KEY not set; can't ingest events", flush=True)
        sys.exit(1)

    lightsei.init(
        api_key=api_key,
        agent_name=agent_name,
        version="0.1.0",
        base_url=base_url,
    )

    print(
        f"polaris up: agent={agent_name} model={MODEL} poll={POLL_S}s "
        f"docs={DOCS_DIR.resolve()} dry_run={DRY_RUN}",
        flush=True,
    )

    while True:
        try:
            tick()
        except Exception:
            print(f"tick crashed:\n{traceback.format_exc()}", flush=True)
        lightsei.flush(timeout=2.0)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
