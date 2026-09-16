from collections.abc import AsyncGenerator
from typing import Annotated

from asyncpg.pool import Pool, PoolConnectionProxy
from fastapi import Depends, Request


async def get_dify_connection(request: Request) -> AsyncGenerator[PoolConnectionProxy]:
    dify_pool: Pool = request.state["dify_pool"]
    async with dify_pool.acquire() as conn:
        yield conn


async def get_nuhs_connection(request: Request) -> AsyncGenerator[PoolConnectionProxy]:
    nuhs_pool: Pool = request.state["nuhs_pool"]
    async with nuhs_pool.acquire() as conn:
        yield conn
        
DifyConnDep = Annotated[PoolConnectionProxy, Depends(get_dify_connection, scope="request")]
NuhsConnDep = Annotated[PoolConnectionProxy, Depends(get_nuhs_connection, scope="request")]