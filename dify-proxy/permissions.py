import logging
from collections.abc import Iterable
from textwrap import dedent

from asyncpg import Record
from asyncpg.pool import PoolConnectionProxy


logger = logging.getLogger(__name__)


async def get_gateway_permissions_by_user_id(
    dify_conn: PoolConnectionProxy, nuhs_conn: PoolConnectionProxy, user_id: str
) -> Iterable[str]:
    user_email = await _get_email_by_user_id(dify_conn, user_id)

    is_admin = await _is_admin(nuhs_conn, user_email)
    if is_admin:
        permissions = ["all"]
    else:
        permissions = list(await _get_permissions_by_email(nuhs_conn, user_email))

    log = logger.info if permissions else logger.warning
    log(
        "Gateway permissions resolved: user_id=%s email=%s is_admin=%s "
        "permissions=%s",
        user_id,
        user_email,
        is_admin,
        permissions,
    )

    return permissions


async def _get_email_by_user_id(dify_conn: PoolConnectionProxy, user_id: str) -> str:
    SELECT_USER_EMAIL_SQL = dedent("""
    SELECT session_id
    FROM end_users
    WHERE id = $1::uuid
    LIMIT 1
    """)
    user_email: str = await dify_conn.fetchval(SELECT_USER_EMAIL_SQL, user_id)
    return user_email


async def _is_admin(nuhs_conn: PoolConnectionProxy, user_email: str) -> bool:
    GET_KM_ROLE_SQL = dedent("""
    SELECT account_role
    FROM authorized_user
    WHERE 1=1
    AND LOWER(email) = LOWER($1)
    AND is_current
    """)
    role = await nuhs_conn.fetchval(GET_KM_ROLE_SQL, user_email)
    return role == "admin"

async def _get_permissions_by_email(
    nuhs_conn: PoolConnectionProxy, user_email: str
) -> Iterable[str]:
    GET_USER_PERMISSIONS_SQL = dedent("""
    SELECT DISTINCT dataset
    FROM data_insight_user_access
    WHERE 1=1
    AND dataset IS NOT NULL
    AND LOWER(user_email) = LOWER($1)
    AND is_deleted = false
    """)
    permission_records: list[Record] = await nuhs_conn.fetch(
        GET_USER_PERMISSIONS_SQL, user_email
    )
    permissions = [record["dataset"] for record in permission_records]

    return permissions
