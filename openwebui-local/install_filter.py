#!/usr/bin/env python3
"""Idempotently install the AgentCore file-context filter in OpenWebUI."""

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from open_webui.models.functions import FunctionForm, Functions
from open_webui.models.users import Users


FUNCTION_ID = "agentcore_file_context"
FUNCTION_NAME = "AgentCore Chat File Context"
FUNCTION_PATH = Path(
    "/opt/agentcore-openwebui/functions/agentcore_file_context.py"
)
DATABASE_PATH = Path("/app/backend/data/webui.db")


def backup_database() -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = DATABASE_PATH.with_name(
        f"{DATABASE_PATH.name}.pre-{FUNCTION_ID}-{timestamp}"
    )
    with (
        sqlite3.connect(DATABASE_PATH) as source,
        sqlite3.connect(destination) as target,
    ):
        source.backup(target)
    return destination


async def install() -> None:
    content = FUNCTION_PATH.read_text()
    compile(content, str(FUNCTION_PATH), "exec")

    admin = await Users.get_super_admin_user() or await Users.get_first_user()
    if not admin:
        raise RuntimeError("OpenWebUI has no administrator account")

    existing = await Functions.get_function_by_id(FUNCTION_ID)
    if not existing:
        backup = backup_database()
        created = await Functions.insert_new_function(
            admin.id,
            "filter",
            FunctionForm(
                id=FUNCTION_ID,
                name=FUNCTION_NAME,
                content=content,
                meta={
                    "description": (
                        "Isolates background tasks and forwards owned chat files"
                    )
                },
            ),
        )
        if not created:
            raise RuntimeError("OpenWebUI did not create the AgentCore filter")
        action = f"installed (database backup: {backup.name})"
    else:
        action = "updated"

    updated = await Functions.update_function_by_id(
        FUNCTION_ID,
        {
            "user_id": admin.id,
            "name": FUNCTION_NAME,
            "type": "filter",
            "content": content,
            "meta": {
                "description": (
                    "Isolates background tasks and forwards owned chat files"
                )
            },
            "is_active": True,
            "is_global": True,
        },
    )
    if not updated:
        raise RuntimeError("OpenWebUI did not activate the AgentCore filter")
    print(f"{FUNCTION_ID}: {action}; active global filter")


if __name__ == "__main__":
    asyncio.run(install())
