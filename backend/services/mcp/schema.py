"""MCP Tool → OpenAI tools schema 转换 + 远程工具命名规则。

命名约定：mcp__{server}__{tool}
- 前缀 mcp__ 是路由判据：loop.py 见此前缀走 MCP，见名知意；
- server 名做命名空间，防止远程工具和本地 TOOL_REGISTRY 重名。
"""
import re
import copy

MCP_PREFIX = "mcp__"

# 工具名允许字母数字 _ - .（MCP 规范建议的字符集，社区 server 有用点的）
_REMOTE_RE = re.compile(r"^mcp__([a-zA-Z0-9_.-]+)__([a-zA-Z0-9_.-]+)$")


def remote_tool_name(server: str, tool: str) -> str:
    """拼出全局唯一的远程工具名。"""
    return f"{MCP_PREFIX}{server}__{tool}"


def parse_remote_name(name: str) -> tuple[str, str] | None:
    """mcp__a__b → ("a", "b")；不是 MCP 工具名返回 None（路由判据）。"""
    m = _REMOTE_RE.match(name)
    return (m.group(1), m.group(2)) if m else None


# 数据载体键：这些键下面装的是"值"不是"schema"，递归时不能进去清
# （如 enum: [{"title": "高"}] 里的 title 是业务数据，不是元数据）
_DATA_KEYS = {"enum", "const", "default", "examples"}


def _strip_unsupported(node) -> None:
    """递归删除 JSON Schema 里 OpenAI 不认的字段（就地修改，调用前先 deepcopy）。

    dict 分支：删本层脏字段，再对 schema 子树递归（properties/items/anyOf/$defs）；
    list 分支：anyOf/oneOf/allOf/required 统一处理；
    _DATA_KEYS 里的值是数据不是 schema，跳过不递归。
    """
    if isinstance(node, dict):
        node.pop("title", None)
        node.pop("$schema", None)
        for key, value in node.items():
            if key not in _DATA_KEYS:
                _strip_unsupported(value)
    elif isinstance(node, list):
        for item in node:
            _strip_unsupported(item)


def to_openai_tool(server: str, tool) -> dict:
    """MCP Tool {name, description, inputSchema} → OpenAI function calling 格式。

    parameters 直搬 inputSchema（都是 JSON Schema），递归清洗 OpenAI 不认的
    字段（title/$schema）；description 可能为 None，兜底空串。
    """
    # deepcopy 脱离原始数据——递归清理是就地修改，浅拷贝会污染 tool.inputSchema
    schema = copy.deepcopy(tool.inputSchema or {})
    _strip_unsupported(schema)
    return {
        "type": "function",
        "function": {
            "name": remote_tool_name(server, tool.name),
            "description": tool.description or "",
            "parameters": schema,
        },
    }
