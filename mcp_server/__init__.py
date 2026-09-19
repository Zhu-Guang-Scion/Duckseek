"""MCP server package exposing nl2data to AI hosts (milestone 5, T17).

Three read-only tools over stdio: ``nl2data_status`` / ``nl2data_list_tables``
/ ``nl2data_ask`` (goals.md decision 9). Credentials stay in environment
variables and never appear in tool arguments, return values or errors.
"""

from mcp_server.server import build_server, serve

__all__ = ["build_server", "serve"]
