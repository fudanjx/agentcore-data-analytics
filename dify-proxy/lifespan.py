from contextlib import asynccontextmanager # noqa: I001

import asyncpg
from fastapi import FastAPI

from model_usage import _database_url

@asynccontextmanager
async def lifespan(app: FastAPI):
    lifespan_state = {}
    # Initialize the asyncpg connection pool to prevent db overload
    dify_async_pool = await asyncpg.create_pool(
        _database_url("dify"),
        min_size=5,
        max_size=10,
    )
    lifespan_state["dify_pool"] = dify_async_pool
    nuhs_async_pool = await asyncpg.create_pool(
        _database_url("nuhs"),
        min_size=5,
        max_size=10
    )
    lifespan_state["nuhs_pool"] = nuhs_async_pool
    yield lifespan_state
    # Teardown actions
    await dify_async_pool.close()
    await nuhs_async_pool.close()