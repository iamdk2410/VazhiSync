"""The Postgres checkpointer for the workflow graph (B12): paused runs survive restarts.

LangGraph's tables live in their own `langgraph` schema, outside Alembic's view (Alembic only
manages `public`), so `alembic check` never sees them and LangGraph manages its own migrations.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import make_url

SCHEMA = "langgraph"


def psycopg_conninfo(database_url: str) -> str:
    """postgresql+asyncpg://... (SQLAlchemy) -> postgresql://... (psycopg)."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


@asynccontextmanager
async def postgres_checkpointer(
    database_url: str, max_size: int = 5
) -> AsyncIterator[AsyncPostgresSaver]:
    conninfo = psycopg_conninfo(database_url)
    async with await AsyncConnection.connect(conninfo, autocommit=True) as conn:
        await conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    pool = AsyncConnectionPool(
        conninfo,
        max_size=max_size,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": f"-c search_path={SCHEMA}",
        },
    )
    await pool.open()
    try:
        saver = AsyncPostgresSaver(pool)  # type: ignore[arg-type]
        await saver.setup()
        yield saver
    finally:
        await pool.close()
