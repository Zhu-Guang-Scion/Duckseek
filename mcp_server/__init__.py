"""DuckSeek MCP server package exposing the nl2data engine to AI hosts (M5).

Three read-only tools over stdio: ``duckseek_status`` / ``duckseek_list_tables``
/ ``duckseek_ask`` (goals.md decision 9; product surface renamed to duckseek
— the CLI/Python package remain ``nl2data``). Credentials stay in environment
variables and never appear in tool arguments, return values or errors.
"""

from mcp_server.server import build_server, serve

__all__ = ["build_server", "serve"]
