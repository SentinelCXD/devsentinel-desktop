"""MCP Client 自检：按 MCP_SERVERS 配置连接所有 server，列出工具并试调一个。

用法：.venv/bin/python check_mcp.py
排查"后端连不上某个 MCP server"时先跑这个——不用起整个 FastAPI。
"""
import asyncio

from services.mcp.client import MCPClientManager
from services.mcp.config import MCP_SERVERS


async def main():
    m = MCPClientManager(MCP_SERVERS)
    await m.connect_all()
    try:
        schemas = m.schemas()
        # 按 server 前缀分组统计
        by_server: dict[str, int] = {}
        for s in schemas:
            server = s["function"]["name"].split("__")[1]
            by_server[server] = by_server.get(server, 0) + 1
        for server, count in by_server.items():
            print(f"{server}: {count} 个工具")
        for s in schemas:
            print(" ", s["function"]["name"])

        # 试调 filesystem 的 list_allowed_directories（无参数，最安全）
        if "mcp__filesystem__list_allowed_directories" in {
            s["function"]["name"] for s in schemas
        }:
            out = await m.call("mcp__filesystem__list_allowed_directories", {})
            print("call result:", out[:200])
    finally:
        await m.close_all()


asyncio.run(main())
