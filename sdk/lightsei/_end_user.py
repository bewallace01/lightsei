"""Phase 25.5: end_user context accessor.

One public surface:

  lightsei.end_user   — read-only accessor for the bot to ask
                         "who is the signed-in customer for this
                         turn?" without coupling to the widget
                         orchestrator. Properties: id, email,
                         display_name, is_identified.

Implementation:

- A ContextVar holds the active _EndUserContext for the duration of
  a single widget.chat handler invocation. Outside that scope (most
  of a bot's lifetime), `is_identified` is False and the other
  properties are None.
- The widget.chat bridge in _chat.py reads the `end_user` field
  from the Command payload (added by backend/widget_orchestrator.py
  in Phase 25.5) and sets the ContextVar around the user-registered
  @on_chat("widget") handler call, then resets.
- Anonymous turns (conversation has no end_user_id) leave the
  ContextVar unset. Bots can branch on `lightsei.end_user.is_identified`.

PII redaction: the `email` property returns the raw address ONLY
when the bot is in the `'pii'` trust zone. For public / internal /
sensitive bots the property returns None even when an end user IS
identified, so a bot that logs `lightsei.end_user.email` can't leak
PII downstream by accident. The `id` and `display_name` properties
do not redact (id is non-identifying, display_name is operator-
chosen and considered safe to expose).

Backwards-compatible: any bot that doesn't reference
`lightsei.end_user` works unchanged. Existing fixtures don't need
updates; the bridge only sets the context when the orchestrator
passes an `end_user` field on the payload, which only happens for
widget conversations scoped to an identified end user (Phase 25.4).
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Optional


# Trust-zone vocabulary mirror. Same string set the backend uses for
# Agent.sensitivity_level. Reproduced here (not imported from the
# backend) so the SDK stays standalone.
_PII_ZONE = "pii"


@dataclass(frozen=True)
class _EndUserContext:
    """Snapshot of identity + sensitivity for one widget.chat turn."""
    id: str
    email: Optional[str]
    display_name: Optional[str]
    # The bot's `sensitivity_level` at dispatch time. Used by the
    # accessor's `email` redaction. Snapshotted at dispatch (not read
    # at access time) so an agent-config flip mid-handler doesn't
    # change what the bot sees.
    sensitivity_hint: str


_current_end_user_ctx: contextvars.ContextVar[Optional[_EndUserContext]] = (
    contextvars.ContextVar("lightsei_end_user_ctx", default=None)
)


class _EndUserAccessor:
    """The object exposed as `lightsei.end_user`.

    A class (not a module-level dict) so attribute access reads the
    ContextVar on every call. That keeps the accessor coherent across
    threads + asyncio tasks without the bot author having to think
    about it.

    Outside a widget.chat handler dispatch (which is most of a bot's
    life), `is_identified == False` and the other properties are
    None. A bot that wants to branch on "do I know who this is?"
    just checks `lightsei.end_user.is_identified`.
    """

    @property
    def is_identified(self) -> bool:
        return _current_end_user_ctx.get() is not None

    @property
    def id(self) -> Optional[str]:
        ctx = _current_end_user_ctx.get()
        return ctx.id if ctx is not None else None

    @property
    def email(self) -> Optional[str]:
        """Email address, redacted unless the bot is in the 'pii' zone.

        Bots configured in public / internal / sensitive zones get
        None even when an end user IS signed in, so accidental
        logging of `lightsei.end_user.email` can't leak PII into a
        zone that wasn't approved for it.
        """
        ctx = _current_end_user_ctx.get()
        if ctx is None:
            return None
        if ctx.sensitivity_hint != _PII_ZONE:
            return None
        return ctx.email

    @property
    def display_name(self) -> Optional[str]:
        """Operator-chosen friendly name, safe to surface to the bot
        in any zone (the end user picked it themselves so it's not
        treated as latent PII)."""
        ctx = _current_end_user_ctx.get()
        return ctx.display_name if ctx is not None else None

    def __repr__(self) -> str:  # pragma: no cover, cosmetic
        if self.is_identified:
            return f"<lightsei.end_user id={self.id!r}>"
        return "<lightsei.end_user anonymous>"


end_user = _EndUserAccessor()


def _set_end_user_context(ctx: _EndUserContext) -> contextvars.Token:
    return _current_end_user_ctx.set(ctx)


def _reset_end_user_context(token: contextvars.Token) -> None:
    _current_end_user_ctx.reset(token)
