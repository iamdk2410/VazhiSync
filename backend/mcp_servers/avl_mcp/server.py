"""avl-mcp: vehicle positions, headways, route shape. Tools arrive in B7.

Run: `python -m mcp_servers.avl_mcp.server` (port 8101).
"""

from mcp.server.mcpserver import MCPServer

from mcp_servers.common import add_health_route, serve

SERVICE_NAME = "avl-mcp"
PORT = 8101

server = MCPServer(SERVICE_NAME, instructions="Vehicle location (AVL) tools for one MTC route.")
add_health_route(server)

if __name__ == "__main__":
    serve(server, SERVICE_NAME, PORT)
