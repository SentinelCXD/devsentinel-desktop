"""services/mcp 的测试：schema 转换纯函数 + Manager 连真实 mcp_server.py。

集成测试用自己的 Server 当 Client 的测试对象——同一份 tools.py，
一条路径本地直调，一条路径走完整 MCP 协议。
"""
import asyncio
from types import SimpleNamespace

from services.mcp import schema
from services.mcp.client import MCPClientManager
from services.mcp.config import BACKEND_DIR


# ---------------------------------------------------------------------------
# schema.py：命名规则 + OpenAI schema 转换（纯函数）
# ---------------------------------------------------------------------------

def test_remote_tool_name_has_prefix():
    assert schema.remote_tool_name("github", "search") == "mcp__github__search"


def test_parse_remote_name_roundtrip():
    assert schema.parse_remote_name("mcp__github__search") == ("github", "search")


def test_parse_remote_name_rejects_non_mcp():
    assert schema.parse_remote_name("search_code") is None       # 本地工具名
    assert schema.parse_remote_name("mcp__onlyserver") is None   # 缺工具段
    assert schema.parse_remote_name("") is None


def test_to_openai_tool_shape():
    tool = SimpleNamespace(
        name="search_code",
        description="搜代码",
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "title": "SearchCodeArgs",   # OpenAI 不认，必须清掉
            "$schema": "http://json-schema.org/draft-07/schema#",
        },
    )
    out = schema.to_openai_tool("devsentinel", tool)

    assert out["type"] == "function"
    fn = out["function"]
    assert fn["name"] == "mcp__devsentinel__search_code"
    assert fn["description"] == "搜代码"
    params = fn["parameters"]
    assert params["properties"]["query"]["type"] == "string"
    assert params["required"] == ["query"]
    assert "title" not in params and "$schema" not in params


def test_to_openai_tool_none_description():
    tool = SimpleNamespace(name="t", description=None, inputSchema={})
    assert schema.to_openai_tool("s", tool)["function"]["description"] == ""


def test_strip_unsupported_is_recursive_and_preserves_data():
    """深层 title/$schema 都清掉；但 enum 里的 title 是数据，不能误删。"""
    tool = SimpleNamespace(
        name="t", description="d",
        inputSchema={
            "type": "object",
            "title": "Root",
            "$schema": "x",
            "properties": {
                "level1": {
                    "type": "object",
                    "title": "L1",
                    "properties": {
                        "level2": {"type": "string", "title": "L2"},
                    },
                },
                "choice": {
                    "type": "string",
                    "enum": [{"title": "业务数据"}],   # 数据里的 title 要保留
                },
            },
        },
    )
    params = schema.to_openai_tool("s", tool)["function"]["parameters"]

    assert "title" not in params and "$schema" not in params
    l1 = params["properties"]["level1"]
    assert "title" not in l1
    assert "title" not in l1["properties"]["level2"]
    # enum 里的 title 是数据，必须原样保留
    assert params["properties"]["choice"]["enum"] == [{"title": "业务数据"}]
    # 原始 inputSchema 不被污染（deepcopy 生效）
    assert tool.inputSchema["title"] == "Root"


# ---------------------------------------------------------------------------
# client.py：连真实 mcp_server.py（spawn 子进程 + initialize + list + call）
# ---------------------------------------------------------------------------

def _own_server_config():
    return [{
        "name": "devsentinel",
        "command": str(BACKEND_DIR / ".venv" / "bin" / "python"),
        "args": [str(BACKEND_DIR / "mcp_server.py")],
    }]


def test_manager_connect_list_call_own_server():
    mgr = MCPClientManager(_own_server_config())

    async def run():
        await mgr.connect_all()
        try:
            names = [s["function"]["name"] for s in mgr.schemas()]
            assert "mcp__devsentinel__search_code" in names
            assert "mcp__devsentinel__git_show" in names

            out = await mgr.call("mcp__devsentinel__search_code", {"query": "POOL_MAX"})
            assert "POOL_MAX" in out

            # 非法名 / 不存在的 server：错误是 observation 字符串，不抛异常
            assert "非法" in await mgr.call("not_mcp_name", {})
            assert "不可用" in await mgr.call("mcp__ghost__tool", {})
        finally:
            await mgr.close_all()

    asyncio.run(run())


def test_manager_survives_bad_server_config():
    """一个 server 连不上不影响 schemas() 可用、其他调用不崩。"""
    mgr = MCPClientManager([{
        "name": "bad", "command": "/nonexistent/python", "args": [],
    }])

    async def run():
        await mgr.connect_all()   # 不应抛异常
        assert mgr.schemas() == []
        assert "不可用" in await mgr.call("mcp__bad__x", {})
        await mgr.close_all()

    asyncio.run(run())
