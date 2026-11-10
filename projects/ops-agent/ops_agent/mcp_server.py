"""MCP server over stdio: the same tools and the same policy, for Claude Desktop, Claude Code or any MCP client.

A call above a limit is not run: it lands in the panel's approval queue, exactly like the in-app agent's calls.

    {"mcpServers": {"shop-ops": {"command": "docker", "args": ["compose", "-f", "/path/to/ops-agent/docker-compose.yml",
                                  "exec", "-T", "web", "python", "-m", "ops_agent.mcp_server"]}}}
"""
import asyncio, json, os

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import db as store
from . import tools as toolbox

ACTOR = os.environ.get("MCP_ACTOR", "MCP client")


def build_server(db_path: str | None = None) -> Server:
    conn = store.connect(db_path)

    async def list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[types.Tool(name=t.name, description=t.description, input_schema=t.schema())
                                            for t in toolbox.TOOLS.values()])

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        out = toolbox.call(conn, params.name, params.arguments or {}, actor=ACTOR)
        if out["status"] == "error":
            text, error = out["error"], True
        elif out["status"] == "needs_approval":
            text, error = (f"Not done yet: {out['reason']}. A manager must approve request #{out['approval_id']} "
                           "in the Ops Agent panel (Approvals)."), False
        else:
            text, error = json.dumps(out["result"], default=str, indent=1), False
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=error)

    return Server("shop-ops", instructions="Back-office tools for the demo shop. Writes above set limits wait for a manager.",
                  on_list_tools=list_tools, on_call_tool=call_tool)


async def main() -> None:
    server = build_server()
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
