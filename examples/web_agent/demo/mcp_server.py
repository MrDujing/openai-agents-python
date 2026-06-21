from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Web Agent Demo MCP")


@mcp.tool()
def lookup_demo_policy(topic: str) -> str:
    """Return demo policy guidance for a supported topic."""
    normalized = topic.strip().lower()
    if "approval" in normalized:
        return "Demo approval policy: shell tool and MCP tool calls should request approval for write or external actions."
    if "session" in normalized:
        return "Demo session policy: keep one session per task, compact long sessions, and clear obsolete sessions."
    if "mcp" in normalized:
        return "Demo MCP policy: prefer local stdio MCP servers for offline demos and require approval for risky tools."
    return "No demo policy is available for that topic. Try approval, session, or MCP."


@mcp.tool()
def summarize_status(system: str, status: str) -> str:
    """Format a short status line for the web agent demo."""
    return f"{system.strip()}: {status.strip()}"


if __name__ == "__main__":
    mcp.run()
