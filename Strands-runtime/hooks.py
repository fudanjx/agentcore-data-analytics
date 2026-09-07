from typing import Any

from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry


class DataToolsPermissionGate(HookProvider):
    def __init__(self, data_tools_mcp_prefix: str = "s3tables"):
        self.data_tools_mcp_prefix = data_tools_mcp_prefix
        
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.validate_permissions)

    def validate_permissions(self, event: BeforeToolCallEvent) -> None:
        user_gateway_permissions: list[str] = event.invocation_state.get("user_gateway_permissions", [])
        tool_name = event.tool_use["name"]
        if tool_name.casefold().startswith(self.data_tools_mcp_prefix) and "all" not in user_gateway_permissions:
            tool_input: dict[str, Any] = event.tool_use["input"]
            if (data_source := tool_input.get("source")) and data_source not in user_gateway_permissions:
                    event.cancel_tool = (
                        f"[ACCESS DENIED] The user does not have permission to view/list/read any data from the source: {data_source}. "
                        f"Do not not try to retrieve any data/tables using {data_source} as source."
                        f"The user only has access to the following sources: {", ".join(user_gateway_permissions)}"
                    )