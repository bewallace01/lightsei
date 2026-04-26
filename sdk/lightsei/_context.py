import contextvars
from typing import Optional

_current_run_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "lightsei_run_id", default=None
)


def get_run_id() -> Optional[str]:
    return _current_run_id.get()


def _set_run_id(run_id: Optional[str]) -> contextvars.Token:
    return _current_run_id.set(run_id)


def _reset_run_id(token: contextvars.Token) -> None:
    _current_run_id.reset(token)
