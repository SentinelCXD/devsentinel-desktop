"""MCP Client：按配置连接 MCP Server，把远程工具并入 Agent 的工具列表。

生命周期：connect_all() 在 FastAPI lifespan 里调一次，session 常驻复用；
close_all() 在 shutdown 调用。单个 server 连接失败不拖垮其他（优雅降级）。

调用哲学与 tools.py 一致：错误是 observation（返回字符串），不是异常。
"""
import json
import logging
import os
import re
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .config import MCP_SERVERS
from .schema import parse_remote_name, remote_tool_name, to_openai_tool

# 日志记录器
log = logging.getLogger(__name__)


class _ServerConn:
    """一条到 MCP Server 的常驻连接：stdio 子进程 + ClientSession + 工具列表。"""

    def __init__(self, name: str):
        self.name = name
        self.session: ClientSession | None = None
        self.tools: list = []
        # AsyncExitStack 统一管理 stdio_client 和 ClientSession 两层上下文，
        # close_all 时一次性逆序清理（先 session 后子进程）
        self.stack = AsyncExitStack()


class MCPClientManager:
    """管理所有配置的 MCP Server 连接，提供 schema 聚合和工具调用路由。"""

    def __init__(self, configs: list[dict] | None = None):
        self._configs = configs if configs is not None else MCP_SERVERS
        self._conns: dict[str, _ServerConn] = {}
        self._schemas: list[dict] = []
        # 远程工具权限级别：mcp__{server}__{tool} → read/write/danger
        self._levels: dict[str, str] = {}

    async def connect_all(self) -> None:
        """逐个连接配置的 server：spawn 子进程 → initialize → list_tools 缓存 schema。"""
        for cfg in self._configs:
            conn = _ServerConn(cfg["name"])
            try:
                params = StdioServerParameters(
                    command=cfg["command"],
                    args=cfg.get("args", []),
                    env=cfg.get("env"),   # 社区 server 常要 API Key 等环境变量
                )
                # stdio_client 建立管道，返回读写流
                read, write = await conn.stack.enter_async_context(stdio_client(params))
                conn.session = await conn.stack.enter_async_context(
                    ClientSession(read, write)
                )
                # 初始化连接，JSON-RPC 2.0 协议 握手，交换能力信息
                await conn.session.initialize()
                # 获取工具列表，MCP规范要求必须 initialize 后才能 list_tools
                conn.tools = (await conn.session.list_tools()).tools
                # 暴露前检查：拼出 mcp__{server}__{tool} 完整名后校验。
                # OpenAI function name 只允许 [a-zA-Z0-9_-] 且总长 ≤64，
                # 必须检查"拼完前缀的名字"而不是原始 tool name——
                # 前缀和 server 名也占长度额度。不合规的工具直接不暴露。
                conn.tools = [
                    t for t in conn.tools
                    if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", remote_tool_name(cfg["name"], t.name or ""))
                    or log.warning("MCP server %s 工具名不合规，跳过: %s", cfg["name"], t.name)
                ]
            except Exception as e:
                log.warning("MCP server %s 连接失败: %s", cfg["name"], e)
                await conn.stack.aclose()
                continue
            self._conns[cfg["name"]] = conn
            # MCP_SCHEMA_DEBUG=1 时逐工具打印转换前后 schema，调试用
            for t in conn.tools:
                remote = remote_tool_name(cfg["name"], t.name)
                converted = to_openai_tool(cfg["name"], t)
                if os.environ.get("MCP_SCHEMA_DEBUG"):
                    print(json.dumps({
                        "server": cfg["name"],
                        "raw": t.model_dump(),
                        "converted": converted,
                    }, ensure_ascii=False))
                self._schemas.append(converted)
                self._levels[remote] = _level_of(t)
            log.info("MCP server %s 已连接，%d 个工具", cfg["name"], len(conn.tools))

    async def close_all(self) -> None:
        # stdio_client 的 anyio task group 在部分 Python/anyio 组合下
        # aclose 会抛 cancel scope 错误（CancelledError 继承自 BaseException，
        # 不在 Exception 里）——关闭是 best-effort，单个失败不影响其他
        for conn in self._conns.values():
            try:
                await conn.stack.aclose()
            except BaseException as e:
                log.warning("MCP server %s 关闭异常: %s", conn.name, e)
        self._conns.clear()
        self._schemas.clear()
        self._levels.clear()

    def schemas(self) -> list[dict]:
        """已连接 server 的 OpenAI tools schema 列表（连接时构建并缓存）。"""
        return list(self._schemas)

    def level(self, remote_name: str) -> str:
        """远程工具的权限级别；未收录的按 'write' 处理（fail-closed）。"""
        return self._levels.get(remote_name, "write")

    async def call(self, remote_name: str, arguments: dict) -> str:
        """调用远程工具：解析 mcp__ 前缀 → 找连接 → tools/call → 拼文本结果。"""
        parsed = parse_remote_name(remote_name)
        if parsed is None:
            return f"[错误] 非法 MCP 工具名: {remote_name}"
        server, tool = parsed
        conn = self._conns.get(server)
        if conn is None or conn.session is None:
            return f"[错误] MCP 服务不可用: {server}"
        try:
            result = await conn.session.call_tool(tool, arguments)
        except Exception as e:
            return f"[错误] MCP 调用失败: {type(e).__name__}: {e}"
        # CallToolResult.content 是 ContentBlock 列表，取文本部分拼接；
        # isError=True 时内容同样是 observation 文本，照常返回给模型
        texts = [c.text for c in result.content if getattr(c, "text", None)]
        return "\n".join(texts) or "[空结果]"


def _level_of(tool) -> str:
    """从 MCP Tool.annotations 推断权限级别。

    readOnlyHint → read；destructiveHint → danger；其余/无注解 → write。
    无注解默认 write 是 fail-closed：server 没声明就当它有副作用。
    """
    ann = getattr(tool, "annotations", None)
    if ann is not None and getattr(ann, "readOnlyHint", False):
        return "read"
    if ann is not None and getattr(ann, "destructiveHint", False):
        return "danger"
    return "write"
