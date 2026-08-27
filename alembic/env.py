import asyncio
import os
import sys
# Ensure project root (/app) is on PYTHONPATH for `import db` when alembic runs in container
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from logging.config import fileConfig
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from db.base import Base
import db.models  # noqa

target_metadata = Base.metadata

def _get_url():
    import os
    env_url = os.getenv("DATABASE_URL", "")
    if env_url:
        # Railway provides postgresql://, but app needs postgresql+asyncpg://
        if env_url.startswith("postgresql://"):
            env_url = env_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return env_url
    return config.get_main_option("sqlalchemy.url")

def run_migrations_offline() -> None:
    url = _get_url()
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()

def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()

async def run_async_migrations() -> None:
    url = _get_url()
    # override config for async engine
    if url:
        config.set_main_option("sqlalchemy.url", url)
    connectable = async_engine_from_config(config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()

def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
