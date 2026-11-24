"""Structured tool errors: the MCP result is `is_error=True` with a JSON `ToolErrorBody` as text,
so the client (B8) can branch on `code` instead of parsing prose."""

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from mcp_servers.schemas import ErrorCode, ToolErrorBody


def tool_error(
    code: ErrorCode, message: str, **details: str | int | float | bool | None
) -> ToolError:
    return ToolError(ToolErrorBody(code=code, message=message, details=details).model_dump_json())


def parse_tool_error(text: str) -> ToolErrorBody | None:
    """The structured error inside an `is_error` result's text, or None if there isn't one.

    The SDK prefixes the message ("Error executing tool <name>: ..."), so the JSON object is
    taken from the first "{".
    """
    start = text.find("{")
    if start < 0:
        return None
    try:
        return ToolErrorBody.model_validate_json(text[start:])
    except ValidationError:
        return None
