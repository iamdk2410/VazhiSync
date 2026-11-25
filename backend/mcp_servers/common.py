"""Shared bootstrap for the MCP servers: health route + streamable-HTTP run."""

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.observability.logging import configure_logging


def add_health_route(server: MCPServer) -> None:
    @server.custom_route("/health", methods=["GET"], include_in_schema=False)  # type: ignore[untyped-decorator]
    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})


def transport_security(service_name: str) -> TransportSecuritySettings:
    """DNS-rebinding protection: only known host names, and only local browser origins."""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[f"{service_name}:*", "localhost:*", "127.0.0.1:*"],
        # Browser tools on this machine (MCP Inspector, dev pages); foreign origins - the
        # DNS-rebinding attack - stay blocked. Requests without an Origin are not affected.
        allowed_origins=["http://localhost:*", "http://127.0.0.1:*"],
    )


def serve(server: MCPServer, service_name: str, port: int) -> None:
    """Run on 0.0.0.0 with DNS-rebinding protection limited to known host names."""
    configure_logging(service_name)
    security = transport_security(service_name)
    server.run(
        "streamable-http",
        host="0.0.0.0",  # noqa: S104  (inside the container; port mapping controls exposure)
        port=port,
        transport_security=security,
    )
