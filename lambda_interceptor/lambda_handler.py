import copy
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    """
    Lambda function that handles both REQUEST and RESPONSE interceptor types.

    For REQUEST interceptors: logs the MCP method and passes request through unchanged
    For RESPONSE interceptors: passes response through unchanged
    """
    INTERCEPTOR_OUTPUT_FORMAT = {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "body": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "",
                },
            },
            "transformedGatewayResponse": {
                "statusCode": 200,
                "body": {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "<result_content>": "<result_value>",
                    },
                },
            },
        },
    }
    
    # Extract the MCP data from the event
    mcp_data = event.get("mcp", {})

    # Check if this is a REQUEST or RESPONSE interceptor based on presence of gatewayResponse
    if "gatewayResponse" in mcp_data and mcp_data["gatewayResponse"] != None:
        # This is a RESPONSE interceptor
        pass
    else:
        # This is a REQUEST interceptor
        gateway_request = mcp_data.get("gatewayRequest", {})
        request_body = gateway_request.get("body", {})
        request_headers = gateway_request.get("headers", {})
        mcp_method = request_body.get("method", "unknown")

        # Log the MCP method
        logger.info(f"Processing REQUEST interceptor - MCP method: {mcp_method}")

        if mcp_method == "unknown":
            reject_response = copy.deepcopy(INTERCEPTOR_OUTPUT_FORMAT)
            del reject_response["mcp"]["transformedGatewayRequest"]
            reject_response["mcp"]["transformedGatewayResponse"]["statusCode"] = 422
            reject_response["mcp"]["transformedGatewayResponse"]["body"]["result"] = {
                "content": [{"type": "text", "text": "No method type passed."}],
                "isError": True
            }
            return reject_response
        
        elif mcp_method == "tools/call" and (target_source := request_body.get("params", {}).get("arguments", {}).get("source")):
            allowed_sources = request_headers.get("allowed-access", "")
            if isinstance(allowed_sources, list):
                pass
            elif isinstance(allowed_sources, str):
                allowed_sources = [value.strip() for value in allowed_sources.split(",")]
                
            if "all" not in allowed_sources and target_source not in allowed_sources:
                reject_response = copy.deepcopy(INTERCEPTOR_OUTPUT_FORMAT)
                del reject_response["mcp"]["transformedGatewayRequest"]
                reject_response["mcp"]["transformedGatewayResponse"]["statusCode"] = 401
                ACCESS_DENY_TEXT = (
                                    f"Do not try to retrieve any data/tables using {target_source} as source. "
                                    f"The user only has access to the following sources: {', '.join(allowed_sources)}"
                                    if allowed_sources
                                    else "The user has NO accessible datasources. Do not attempt to use any tools that requires a source."
                                )
                reject_response["mcp"]["transformedGatewayResponse"]["body"]["result"] = {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"[ACCESS DENIED] The user does not have permission to view/list/read any data from the source: {target_source}"
                                f"{ACCESS_DENY_TEXT}"
                            )
                        }
                    ],
                    "isError": True
                }
                return reject_response
        
        # Pass through the original request unchanged
        response = {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "body": request_body,
            }
        }
        }

        return response