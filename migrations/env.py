from __future__ import annotations

from logging.config import fileConfig

from alembic import context

import infra
import schema

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# `schema` imports every component, so autogenerate sees the full metadata
# rather than whichever modules happened to be imported first.
target_metadata = schema.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=infra.database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = infra.build_engine()
    with connectable.connect() as connection:
        # SQLite cannot ALTER most things in place; batch mode rewrites the
        # table instead, which is what makes later schema changes possible.
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
