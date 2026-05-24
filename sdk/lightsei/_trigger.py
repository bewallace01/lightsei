"""Phase 22.5: trigger context + @on_trigger decorator.

Two public surfaces:

  lightsei.trigger      — read-only accessor for the bot to ask
                           "how was I invoked?" without coupling to
                           the command dispatch machinery. Properties:
                           kind, scheduled_at, webhook_payload, name.

  @lightsei.on_trigger  — decorator a bot can register to handle
                           scheduled fires from the backend's 22.4
                           scheduled_run job + 22.6 webhook endpoint.
                           Sibling to @on_chat and @on_command.

Implementation:

- A ContextVar holds the active TriggerContext for the duration of a
  trigger.fire dispatch. Outside that scope, the accessor reports
  kind='manual' + all other properties None.
- Importing this module side-effect-registers a bridge handler with
  @on_command('trigger.fire'). The bridge reads the Command payload,
  sets the trigger context (and run_id context, so events emitted
  inside the handler flow into the pre-allocated Run row from 22.4),
  calls the user's @on_trigger handler, and returns a result dict.
- The bridge is a clean no-op when no @on_trigger handler is
  registered, so importing this module is safe for bots that don't
  use scheduled triggers.

Backwards-compatible: any bot that doesn't reference `lightsei.trigger`
or @on_trigger works unchanged. Existing test fixtures don't need any
update; the bridge only runs when the backend dispatches a
'trigger.fire' command, which only happens for triggers the operator
has explicitly created.
"""
from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

logger = logging.getLogger("lightsei.trigger")


@dataclass(frozen=True)
class _TriggerContext:
    kind: str  # 'cron' | 'webhook'
    name: Optional[str]
    scheduled_at: Optional[datetime]
    webhook_payload: Optional[dict[str, Any]]
    trigger_id: Optional[str]


_current_trigger_ctx: contextvars.ContextVar[Optional[_TriggerContext]] = (
    contextvars.ContextVar("lightsei_trigger_ctx", default=None)
)


class _TriggerAccessor:
    """The object exposed as `lightsei.trigger`.

    A class (not a module-level dict) so attribute access reads the
    ContextVar on every call. That keeps the accessor coherent across
    threads + asyncio tasks without the bot author having to think
    about it.

    Outside a trigger.fire dispatch (which is most of a bot's life),
    `kind == 'manual'` and the other properties are None. A bot that
    wants to branch on "was I cron-fired or did someone call me by
    hand" can just check `lightsei.trigger.kind`.
    """

    @property
    def kind(self) -> str:
        ctx = _current_trigger_ctx.get()
        return ctx.kind if ctx is not None else "manual"

    @property
    def name(self) -> Optional[str]:
        ctx = _current_trigger_ctx.get()
        return ctx.name if ctx is not None else None

    @property
    def scheduled_at(self) -> Optional[datetime]:
        ctx = _current_trigger_ctx.get()
        return ctx.scheduled_at if ctx is not None else None

    @property
    def webhook_payload(self) -> Optional[dict[str, Any]]:
        ctx = _current_trigger_ctx.get()
        return ctx.webhook_payload if ctx is not None else None

    @property
    def trigger_id(self) -> Optional[str]:
        ctx = _current_trigger_ctx.get()
        return ctx.trigger_id if ctx is not None else None

    def __repr__(self) -> str:  # pragma: no cover — cosmetic
        return f"<lightsei.trigger kind={self.kind!r} name={self.name!r}>"


trigger = _TriggerAccessor()


# kind -> handler. At most one @on_trigger handler per bot process;
# re-decorating replaces. (Same shape as @on_chat for the default
# channel.)
_handler: Optional[Callable[..., Any]] = None


def on_trigger(arg=None):
    """Register the @on_trigger handler.

    Two call shapes:

      @lightsei.on_trigger
      def handle():
          # lightsei.trigger.kind / .scheduled_at / .webhook_payload
          # are populated for the duration of this call.
          ...

      @lightsei.on_trigger()        # parens, no args — same as above.
      def handle():
          ...

    The handler takes no required arguments. Read `lightsei.trigger.*`
    inside the handler to branch on cron-vs-webhook + access the
    webhook payload (if any). Return value (dict or None) is captured
    as the command result.

    Re-decorating replaces any previously-registered handler.
    """
    if callable(arg):
        # `@on_trigger` (no parens) — arg is the function.
        global _handler
        _handler = arg
        return arg

    # `@on_trigger()` — return a decorator that registers when applied.
    def decorator(fn: Callable[..., Any]):
        global _handler
        _handler = fn
        return fn

    return decorator


def has_trigger_handler() -> bool:
    return _handler is not None


def get_trigger_handler() -> Optional[Callable[..., Any]]:
    return _handler


def _set_trigger_context(ctx: _TriggerContext) -> contextvars.Token:
    return _current_trigger_ctx.set(ctx)


def _reset_trigger_context(token: contextvars.Token) -> None:
    _current_trigger_ctx.reset(token)


# ---------- trigger.fire bridge ---------- #


def _parse_scheduled_at(raw: Any) -> Optional[datetime]:
    """The backend serializes scheduled_at as ISO 8601. Decode best-
    effort; an unparseable value just becomes None so the bot's
    handler doesn't crash on weird inputs."""
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _trigger_fire_bridge(payload: dict[str, Any]) -> dict[str, Any]:
    """Internal: handle a `trigger.fire` command from the 22.4
    scheduled_run handler. Sets the trigger context + run_id context
    (so events emitted inside the handler flow into the pre-allocated
    Run row), calls the user-registered @on_trigger handler, returns
    a structured result.

    Always returns a dict — never raises. A handler exception is
    captured as `{ok: False, error: ...}` and the command is marked
    failed by the SDK poller.
    """
    if _handler is None:
        # Bot doesn't have @on_trigger registered. Surface as a clean
        # error so the operator sees it in the run's events instead
        # of the run silently doing nothing.
        return {
            "ok": False,
            "error": (
                "no @lightsei.on_trigger handler registered; trigger "
                "fired but the bot has nothing to run"
            ),
        }

    # Build the trigger context from the command payload.
    ctx = _TriggerContext(
        kind=payload.get("trigger_kind") or "cron",
        name=payload.get("trigger_name"),
        scheduled_at=_parse_scheduled_at(payload.get("scheduled_at")),
        webhook_payload=payload.get("webhook_payload"),
        trigger_id=payload.get("trigger_id"),
    )

    # Set both the trigger context AND the run_id context so events
    # emitted by the handler flow into the pre-allocated Run row
    # (from 22.4). Local import to keep the module import graph flat.
    from ._context import _set_run_id, _reset_run_id

    run_id = payload.get("run_id")
    trigger_token = _set_trigger_context(ctx)
    run_token = _set_run_id(run_id) if run_id else None

    try:
        try:
            result = _handler()
        except BaseException as exc:
            logger.exception(
                "trigger.fire bridge: @on_trigger handler raised"
            )
            return {"ok": False, "error": repr(exc)}
    finally:
        _reset_trigger_context(trigger_token)
        if run_token is not None:
            _reset_run_id(run_token)

    # Return value: dict, None, or anything else (coerced to a sane
    # shape for the command result).
    if result is None:
        return {"ok": True}
    if isinstance(result, dict):
        return {"ok": True, **result}
    return {"ok": True, "value": result}


def _register_trigger_fire_bridge() -> None:
    """Side-effect: register the bridge with the SDK's command handler
    registry. Idempotent — re-importing _trigger.py is safe. Kept in
    a function so test code can re-register after clearing the registry.
    """
    from ._commands import on_command
    on_command(
        "trigger.fire",
        description=(
            "Internal: scheduled or webhook-fired trigger from the "
            "backend's 22.4 scheduled_run handler. Calls "
            "@lightsei.on_trigger."
        ),
    )(_trigger_fire_bridge)


_register_trigger_fire_bridge()
