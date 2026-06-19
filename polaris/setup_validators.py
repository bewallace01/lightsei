"""Register Polaris's output validators on the calling workspace.

Run this once after deploying Polaris (or any time you change the
validator config):

    LIGHTSEI_API_KEY=bk_... python polaris/setup_validators.py

Registers two validators against `polaris.plan` events:

  schema_strict   The full polaris.plan event payload (the bot-emitted
                  envelope around Claude's submit_plan tool input).
                  Catches malformed payloads, missing required fields,
                  wrong types, and unexpected extra fields.

  content_rules   The default rule pack from the validator module:
                  no email-like patterns in `summary`, no destructive
                  verbs in `next_actions[].task`. The pack is the
                  source of truth — if you want to tweak it, edit
                  backend/validators/content_rules.py and re-run this.

Idempotent: calling it twice just overwrites the existing config rows.
"""
import os
import sys
import urllib.error
import urllib.request
import json
from pathlib import Path

# Lift the default rule pack from the validator module so the demo
# always runs against whatever rules the backend ships with. The
# script can be run from any cwd; resolve relative to itself.
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT / "backend"))
from validators.content_rules import DEFAULT_RULE_PACK  # noqa: E402


# The full polaris.plan event payload schema.
#
# The bot's `submit_plan` tool input_schema (in polaris/bot.py) covers
# the model-produced fields: summary, next_actions, parking_lot_promotions,
# drift. The bot then wraps that in an envelope adding text, doc_hashes,
# model, tokens_in, tokens_out before emitting. This schema validates the
# full envelope.
#
# additionalProperties is True (the JSON-Schema default) so future Polaris
# revisions can add fields without breaking validation. `required` is the
# enforcement knob.
POLARIS_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "doc_hashes": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {"type": "string"},
        },
        "model": {"type": "string"},
        "tokens_in": {"type": "integer"},
        "tokens_out": {"type": "integer"},
        "summary": {"type": "string"},
        "next_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "why": {"type": "string"},
                    "blocked_by": {
                        "anyOf": [{"type": "string"}, {"type": "null"}]
                    },
                },
                "required": ["task", "why", "blocked_by"],
            },
        },
        "parking_lot_promotions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["item", "why"],
            },
        },
        "drift": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "between": {"type": "string"},
                    "observation": {"type": "string"},
                },
                "required": ["between", "observation"],
            },
        },
    },
    "required": [
        "doc_hashes",
        "model",
        "tokens_in",
        "tokens_out",
        "summary",
        "next_actions",
        "parking_lot_promotions",
        "drift",
    ],
}


def _put(base_url: str, api_key: str, event_kind: str, validator_name: str, config: dict) -> dict:
    url = f"{base_url}/workspaces/me/validators/{event_kind}/{validator_name}"
    body = json.dumps({"config": config}).encode()
    req = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise SystemExit(
            f"PUT {url} failed: {e.code} {detail}"
        ) from None


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    if not api_key:
        raise SystemExit("LIGHTSEI_API_KEY env var is required")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")

    print(f"registering validators against {base_url}")

    schema_resp = _put(
        base_url, api_key, "polaris.plan", "schema_strict",
        {"schema": POLARIS_PLAN_SCHEMA},
    )
    print(
        f"  schema_strict   created_at={schema_resp['created_at']} "
        f"updated_at={schema_resp['updated_at']}"
    )

    content_resp = _put(
        base_url, api_key, "polaris.plan", "content_rules",
        {"rules": DEFAULT_RULE_PACK},
    )
    print(
        f"  content_rules   created_at={content_resp['created_at']} "
        f"updated_at={content_resp['updated_at']} "
        f"({len(DEFAULT_RULE_PACK)} rules: "
        f"{', '.join(r['name'] for r in DEFAULT_RULE_PACK)})"
    )
    print("done.")


if __name__ == "__main__":
    main()
