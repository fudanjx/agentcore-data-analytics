#!/usr/bin/env python3
"""Idempotently install the AgentCore filter in the Insights PostgreSQL database."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "/app/backend")

from open_webui.models.functions import FunctionForm, Functions
from open_webui.models.users import Users


FUNCTION_ID = "agentcore_file_context"
FUNCTION_NAME = "AgentCore Chat File Context"
FUNCTION_PATH = Path(
    "/opt/agentcore-openwebui/functions/agentcore_file_context.py"
)


async def install() -> None:
    content = FUNCTION_PATH.read_text()
    compile(content, str(FUNCTION_PATH), "exec")

    admin = await Users.get_super_admin_user() or await Users.get_first_user()
    if not admin:
        raise RuntimeError("OpenWebUI has no administrator account")

    existing = await Functions.get_function_by_id(FUNCTION_ID)
    if not existing:
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
        action = "installed"
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
