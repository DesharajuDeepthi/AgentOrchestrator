"""Stub MCP server for manager tests (spec §11).

FastMCP stdio server with a mutable toolset:
- v1 exposes `echo` and `add`
- calling `mutate_toolset` drops `add`, adds `extra_tool`, and emits a
  tools/list_changed notification
- calling `die` hard-exits the process (health-loop tests)
"""

import asyncio
import os
import sys

from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("stub")


def echo(text: str) -> str:
    """Echo the given text back."""
    return f"echo:{text}"


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def extra_tool() -> str:
    """A tool that only exists in toolset v2."""
    return "extra"


async def mutate_toolset(ctx: Context) -> str:  # type: ignore[type-arg]
    """Switch the server to toolset v2 and notify listChanged."""
    mcp._tool_manager.remove_tool("add")
    mcp.add_tool(extra_tool)
    await ctx.session.send_tool_list_changed()
    return "mutated"


def die() -> str:
    """Hard-exit the server process."""
    os._exit(1)


mcp.add_tool(echo)
mcp.add_tool(add)
mcp.add_tool(mutate_toolset)
mcp.add_tool(die)


if __name__ == "__main__":
    if "--fail" in sys.argv:
        print("boot failure", file=sys.stderr)
        sys.exit(3)
    asyncio.run(mcp.run_stdio_async())
