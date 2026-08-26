from collections.abc import Iterable
from textwrap import dedent

from asyncpg.pool import PoolConnectionProxy


async def get_gateway_permissions_by_user_id(
    dify_conn: PoolConnectionProxy,
    nuhs_conn: PoolConnectionProxy,
    user_id: str
) -> Iterable[str]:
    user_email = await _get_email_by_user_id(dify_conn, user_id)
    permissions = await _get_permissions_by_email(nuhs_conn, user_email)
    
    return permissions

async def _get_email_by_user_id(
    dify_conn: PoolConnectionProxy,
    user_id: str
) -> str:
    SELECT_USER_EMAIL_SQL = dedent("""
    SELECT session_id
    FROM end_users
    WHERE id = $1::uuid
    LIMIT 1
    """
)
    user_email: str = await dify_conn.fetchval(SELECT_USER_EMAIL_SQL, user_id)
    return user_email

async def _get_permissions_by_email(
    nuhs_conn: PoolConnectionProxy,
    user_email: str
) -> Iterable[str]:
    # TODO: Replace with actual permissions evaluation logic
    if "dedric" in user_email:
        permissions = ["ah"]
    else:
        permissions = ["all"]
        
    return permissions