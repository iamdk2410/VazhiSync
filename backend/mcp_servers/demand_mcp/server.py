"""demand-mcp: boarding estimates and the guarded send_driver_instruction. Tools arrive in B7.

Run: `python -m mcp_servers.demand_mcp.server` (port 8102).
"""

from mcp.server.mcpserver import MCPServer

from mcp_servers.common import add_health_route, serve

SERVICE_NAME = "demand-mcp"
PORT = 8102

server = MCPServer(SERVICE_NAME, instructions="Passenger demand and driver dispatch tools.")
add_health_route(server)

if __name__ == "__main__":
    serve(server, SERVICE_NAME, PORT)
