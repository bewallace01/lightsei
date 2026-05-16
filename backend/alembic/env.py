"""Alembic environment.

Uses the same normalized DATABASE_URL that the backend uses at runtime, so
alembic and the app always agree on which driver to load (psycopg 3, never 2).
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from db import DATABASE_URL
from models import Base

config = context.config

if config.config_file_name is not None:
    # `disable_existing_loggers=False` is critical: Python's `fileConfig`
    # defaults to True, which would silently mute every logger that
    # already exists in the process — including uvicorn's access /
    # error loggers, FastAPI's, and our own `lightsei.*` modules. That
    # mute persists for the rest of the process, so all post-startup
    # logs vanish even though the app keeps serving requests. The
    # alembic.ini only configures `root`, `sqlalchemy`, and `alembic`;
    # we want those settings layered on top of uvicorn's, not in place
    # of them.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    return DATABASE_URL


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section, {}) or {}
    cfg["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        cfg, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
