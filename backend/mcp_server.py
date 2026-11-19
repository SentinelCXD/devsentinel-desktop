"""DevSentinel 诊断工具的 MCP Server（stdio transport）。

把 services/agent/tools.py 里已有的确定性工具暴露成标准 MCP tools，
供任意 MCP Host（Windsurf / Claude Desktop / 本项目后端）调用。

两个设计决策：
1. 工具描述直接复用 registry.py 的 TOOL_REGISTRY——描述是打磨过的 prompt 资产，
   只维护一份，MCP 侧不另写，以后改描述不碰这里。
2. 只暴露 Agent 注册的 5 个工具；grep_logs 不暴露——它与 read_log 功能重叠
   （workflow 遗留），暴露重叠工具会让调用方困惑，暴露哪些工具是接口设计决策。

运行（stdio，由 Host 拉起子进程，不手动前台跑）：
    python mcp_server.py
调试：
    npx @modelcontextprotocol/inspector
"""
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from services.agent import tools
from services.agent.registry import TOOL_REGISTRY

mcp = FastMCP("devsentinel-mcp")

# 5 个工具全是只读诊断工具——标注给 MCP Host 看，client 侧据此做权限分级
READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False)


@mcp.tool(description=TOOL_REGISTRY["list_services"]["description"], annotations=READ_ONLY)
def list_services() -> str:
    # read_log 的 service 命名空间入口：Host 先拿清单再查具体服务
    return tools.list_services()


@mcp.tool(description=TOOL_REGISTRY["read_log"]["description"], annotations=READ_ONLY)
def read_log(service: str = "", keywords: list[str] | None = None,
             start: str = "", end: str = "", max_lines: int = 50) -> str:
    # keywords 允许 None（Host 可能显式传 null），底层工具只吃 list
    return tools.read_log(service=service, keywords=keywords or [],
                          start=start, end=end, max_lines=max_lines)


@mcp.tool(description=TOOL_REGISTRY["search_code"]["description"], annotations=READ_ONLY)
def search_code(query: str, max_lines: int = 30) -> str:
    return tools.search_code(query=query, max_lines=max_lines)


@mcp.tool(description=TOOL_REGISTRY["git_log"]["description"], annotations=READ_ONLY)
def git_log(max_count: int = 10) -> str:
    return tools.git_log(max_count=max_count)


@mcp.tool(description=TOOL_REGISTRY["git_show"]["description"], annotations=READ_ONLY)
def git_show(commit: str, max_lines: int = 80) -> str:
    return tools.git_show(commit=commit, max_lines=max_lines)


if __name__ == "__main__":
    mcp.run()  # 默认 stdio transport
