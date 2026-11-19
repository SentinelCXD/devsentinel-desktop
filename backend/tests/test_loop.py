"""agent_loop 的确定性单测：LLM 和工具全部 mock，只验证循环控制逻辑。

被测的不变量（这些错了 Agent 就废了）：
- tool_call / tool_result 按 tool_call_id 配对，顺序与模型给出的 tool_calls 一致
- 重复调用不执行；JSON 参数非法不执行；单个工具异常不影响其他
- messages 回填：assistant 原话 + 每个 tool_call_id 对应一条 role=tool 消息
- max_steps / token 预算 / LLM 异常的收尾事件

mock 点：
- loop.client        → FakeLLM（脚本化响应队列，记录每次请求 kwargs）
- loop.call_tool     → FakeTools（同步函数，loop 内部会 to_thread）
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from services.agent import loop


# ---------------------------------------------------------------------------
# 假对象构造
# ---------------------------------------------------------------------------

def _tc(id_: str, name: str, args: str):
    """伪造一个 tool_call：id + function.name + function.arguments（JSON 字符串）。"""
    return SimpleNamespace(id=id_, function=SimpleNamespace(name=name, arguments=args))


def _resp(content=None, tool_calls=None, total_tokens=100):
    """伪造一次 chat.completions.create 的返回值。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))],
        usage=SimpleNamespace(total_tokens=total_tokens),
    )


class FakeLLM:
    """脚本化 LLM：按队列依次吐响应，元素是 Exception 就抛出。

    requests 记录每次调用的 kwargs——用于断言"第二轮请求里有没有 tool 结果回填"、
    "预算超限后那次调用是否真的不带 tools 参数"。
    """

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeTools:
    """同步假工具（与 call_tool 同签名）：记录调用，可按工具名编程返回/抛错。"""

    def __init__(self, results=None, errors=None):
        self.calls = []
        self.results = results or {}
        self.errors = errors or {}

    def __call__(self, name, args):
        self.calls.append({"name": name, "args": args})
        if name in self.errors:
            raise self.errors[name]
        return self.results.get(name, f"[ok] {name}")


@pytest.fixture
def wire(monkeypatch):
    """把 loop 的两个外部依赖换成假对象，返回 (llm, tools) 供各测试编排。"""
    llm = FakeLLM([])
    tools = FakeTools()
    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=llm.create))
    )
    monkeypatch.setattr(loop, "client", fake_client)
    monkeypatch.setattr(loop, "call_tool", tools)
    # 默认全部工具 read 级——审批分支只在测试显式覆盖 level 时才触发，
    # 否则 unknown 测试工具名会被 fail-closed 成 write 级卡住等审批
    monkeypatch.setattr(loop, "tool_level", lambda name: "read")
    return llm, tools


async def _collect(agen):
    return [e async for e in agen]


def _run(coro):
    return asyncio.run(_collect(coro))


def _types(events):
    return [e["type"] for e in events]


def _tool_msgs(request):
    """从某次 LLM 请求的 messages 里捞 role=tool 的消息。

    注意 messages 里混着两种类型：dict（system/user/tool）和
    SimpleNamespace（assistant 原话，loop 直接 append 了响应对象）。
    """
    return [m for m in request["messages"]
            if isinstance(m, dict) and m.get("role") == "tool"]


# ---------------------------------------------------------------------------
# 基本路径
# ---------------------------------------------------------------------------

def test_answer_without_tool_calls(wire):
    """模型直接给结论：只有 step + content，无任何工具事件。"""
    llm, tools = wire
    llm.script = [_resp(content="根因是连接池打满")]

    events = _run(loop.agent_loop("orders 503"))

    assert _types(events) == ["step", "content"]
    assert events[1]["text"] == "根因是连接池打满"
    assert tools.calls == []


def test_single_tool_call_full_cycle(wire):
    """一轮工具调用的完整链路：事件配对 + messages 回填正确。"""
    llm, tools = wire
    llm.script = [
        _resp(tool_calls=[_tc("call_1", "search_code", '{"query": "POOL_MAX"}')]),
        _resp(content="结论：池子被压到 1"),
    ]

    events = _run(loop.agent_loop("排查"))

    assert _types(events) == ["step", "tool_call", "tool_result", "step", "content"]
    # 事件层：tool_call 和 tool_result 用同一个 id 配对
    assert events[1]["id"] == "call_1" and events[2]["id"] == "call_1"
    assert events[2]["result"] == "[ok] search_code"
    # 协议层：第二次 LLM 请求的 messages 里必须有 role=tool 且 tool_call_id 对上
    tool_msgs = _tool_msgs(llm.requests[1])
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert tool_msgs[0]["content"] == "[ok] search_code"
    # assistant 原话（含 tool_calls）也要回填，否则下一轮协议报错——
    # loop 直接 append 响应 message 对象（SimpleNamespace，带 tool_calls 属性）
    assert any(getattr(m, "tool_calls", None) for m in llm.requests[1]["messages"])


def test_thought_emitted_before_tool_calls(wire):
    """content + tool_calls 同时存在时，先吐 thought 再吐 tool_call。"""
    llm, _ = wire
    llm.script = [
        _resp(content="我先查一下日志", tool_calls=[_tc("c1", "read_log", "{}")]),
        _resp(content="done"),
    ]

    events = _run(loop.agent_loop("q"))

    assert _types(events)[:4] == ["step", "thought", "tool_call", "tool_result"]
    assert events[1]["text"] == "我先查一下日志"


# ---------------------------------------------------------------------------
# 并发与回填顺序
# ---------------------------------------------------------------------------

def test_multiple_tool_calls_keep_order_and_ids(wire):
    """同一轮多个 tool_call：结果按原顺序回填，不按完成顺序。"""
    llm, tools = wire
    tools.results = {"search_code": "R_code", "git_log": "R_git"}
    llm.script = [
        _resp(tool_calls=[
            _tc("a", "search_code", '{"query": "x"}'),
            _tc("b", "git_log", "{}"),
        ]),
        _resp(content="ok"),
    ]

    events = _run(loop.agent_loop("q"))

    results = [e for e in events if e["type"] == "tool_result"]
    assert [(r["id"], r["result"]) for r in results] == [("a", "R_code"), ("b", "R_git")]
    # messages 里的 tool 消息顺序与 tool_calls 一致
    tool_msgs = _tool_msgs(llm.requests[1])
    assert [m["tool_call_id"] for m in tool_msgs] == ["a", "b"]


def test_display_truncated_but_model_gets_full(wire):
    """前端事件截断 2000，回填给模型的必须是完整结果——两份内容有意不同。"""
    llm, tools = wire
    tools.results = {"search_code": "x" * 3000}
    llm.script = [
        _resp(tool_calls=[_tc("c1", "search_code", "{}")]),
        _resp(content="ok"),
    ]

    events = _run(loop.agent_loop("q"))

    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert len(tool_result["result"]) == 2000
    tool_msg = next(m for m in _tool_msgs(llm.requests[1]))
    assert len(tool_msg["content"]) == 3000


# ---------------------------------------------------------------------------
# 去重 / 参数错误 / 异常隔离
# ---------------------------------------------------------------------------

def test_duplicate_call_short_circuits(wire):
    """同一轮两个完全相同的调用：只执行一次，第二个拿到提示不执行。"""
    llm, tools = wire
    llm.script = [
        _resp(tool_calls=[
            _tc("d1", "search_code", '{"query": "POOL_MAX"}'),
            _tc("d2", "search_code", '{"query": "POOL_MAX"}'),
        ]),
        _resp(content="ok"),
    ]

    events = _run(loop.agent_loop("q"))

    assert len(tools.calls) == 1  # 只真执行了一次
    results = {e["id"]: e["result"] for e in events if e["type"] == "tool_result"}
    assert results["d1"] == "[ok] search_code"
    assert "已用相同参数调用过" in results["d2"]


def test_duplicate_call_key_order_still_short_circuits(wire):
    """键序/空白不同的相同参数也算重复：sig 按语义规范化，不是原始字符串。

    回归：原实现用 arguments 原始字符串拼 sig，{"a":1,"b":2} 和
    {"b":2,"a":1} 会被当成两个不同调用放行，导致重复执行。
    """
    llm, tools = wire
    llm.script = [
        _resp(tool_calls=[
            _tc("k1", "search_code", '{"query": "POOL_MAX", "max_lines": 30}'),
            _tc("k2", "search_code", '{"max_lines": 30,  "query": "POOL_MAX"}'),
        ]),
        _resp(content="ok"),
    ]

    events = _run(loop.agent_loop("q"))

    assert len(tools.calls) == 1  # 只真执行了一次
    results = {e["id"]: e["result"] for e in events if e["type"] == "tool_result"}
    assert results["k1"] == "[ok] search_code"
    assert "已用相同参数调用过" in results["k2"]


def test_call_signature_canonicalizes_json():
    """_call_signature 契约：语义相同的 JSON 得到相同 sig；非法 JSON 退回原文。"""
    sig1, args1 = loop._call_signature("t", '{"a": 1, "b": 2}')
    sig2, args2 = loop._call_signature("t", '{"b": 2,  "a": 1}')
    assert sig1 == sig2 and args1 == args2

    sig3, args3 = loop._call_signature("t", "{bad json")
    assert args3 is None and sig3 == "t:{bad json"


def test_malformed_json_args_not_executed(wire):
    """arguments 不是合法 JSON：转成错误 observation，不执行工具。"""
    llm, tools = wire
    llm.script = [
        _resp(tool_calls=[_tc("j1", "search_code", "{bad json")]),
        _resp(content="ok"),
    ]

    events = _run(loop.agent_loop("q"))

    assert tools.calls == []
    result = next(e for e in events if e["type"] == "tool_result")
    assert "不是合法 JSON" in result["result"]


def test_tool_exception_does_not_block_others(wire):
    """一个工具抛异常，另一个照常返回；循环继续走到结论。"""
    llm, tools = wire
    tools.errors = {"bad_tool": RuntimeError("boom")}
    tools.results = {"good_tool": "R_good"}
    llm.script = [
        _resp(tool_calls=[
            _tc("e1", "bad_tool", "{}"),
            _tc("e2", "good_tool", "{}"),
        ]),
        _resp(content="结论"),
    ]

    events = _run(loop.agent_loop("q"))

    results = {e["id"]: e["result"] for e in events if e["type"] == "tool_result"}
    assert "工具执行异常" in results["e1"] and "boom" in results["e1"]
    assert results["e2"] == "R_good"
    assert events[-1]["type"] == "content"


def test_fail_count_hint_after_two_failures(wire):
    """同一工具连续失败两次，第二次结果追加'已多次失败'提示。"""
    llm, tools = wire
    tools.errors = {"flaky": RuntimeError("always fails")}
    # 两轮用不同参数，绕开重复调用去重
    llm.script = [
        _resp(tool_calls=[_tc("f1", "flaky", '{"q": 1}')]),
        _resp(tool_calls=[_tc("f2", "flaky", '{"q": 2}')]),
        _resp(content="放弃，无法获取"),
    ]

    events = _run(loop.agent_loop("q"))

    results = [e for e in events if e["type"] == "tool_result"]
    assert len(results) == 2
    assert "已多次失败" not in results[0]["result"]
    assert "已多次失败" in results[1]["result"]


# ---------------------------------------------------------------------------
# 收尾路径
# ---------------------------------------------------------------------------

def test_max_steps_exceeded(wire):
    """模型一直调工具不收尾：max_steps 耗尽后吐 max_steps 错误事件。"""
    llm, _ = wire
    llm.script = [_resp(tool_calls=[_tc(f"c{i}", "search_code", "{}")]) for i in range(5)]

    events = _run(loop.agent_loop("q", max_steps=2))

    assert _types(events).count("step") == 2
    assert events[-1] == {
        "type": "error", "code": "max_steps",
        "message": "达到最大步数 2，请人工介入", "retryable": False,
    }


def test_llm_exception_emits_error_event(wire):
    """LLM 调用抛异常：吐 llm_call_failed 错误事件后流结束，不崩。"""
    llm, _ = wire
    llm.script = [RuntimeError("api down")]

    events = _run(loop.agent_loop("q"))

    assert _types(events) == ["step", "error"]
    assert events[1]["code"] == "llm_call_failed"
    assert events[1]["retryable"] is True


def test_mcp_prefixed_tool_routes_to_manager(wire, monkeypatch):
    """mcp__ 前缀的工具名走 MCP manager，不走本地 call_tool。"""
    llm, tools = wire
    mcp_calls = []

    class FakeMCP:
        def schemas(self):
            return []

        def level(self, name):
            return "read"

        async def call(self, name, args):
            mcp_calls.append((name, args))
            return "[mcp] ok"

    monkeypatch.setattr(loop, "mcp_manager", FakeMCP())
    llm.script = [
        _resp(tool_calls=[_tc("m1", "mcp__devsentinel__search_code", '{"query": "x"}')]),
        _resp(content="done"),
    ]

    events = _run(loop.agent_loop("q"))

    assert mcp_calls == [("mcp__devsentinel__search_code", {"query": "x"})]
    assert tools.calls == []  # 本地工具没被碰
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["result"] == "[mcp] ok"


def test_token_budget_forces_final_without_tools(wire):
    """token 超预算：追加 system 提示后再调一次模型，且这次不带 tools 参数。"""
    llm, _ = wire
    llm.script = [
        _resp(tool_calls=[_tc("c1", "search_code", "{}")], total_tokens=99999),
        _resp(content="基于已有信息的结论"),
    ]

    events = _run(loop.agent_loop("q"))

    assert len(llm.requests) == 2
    assert "tools" not in llm.requests[1]          # 收尾调用不给工具，强制模型说话
    assert any(m["role"] == "system" and "预算" in m["content"] for m in llm.requests[1]["messages"])
    content = next(e for e in events if e["type"] == "content")
    assert content["text"].startswith("[已达 token 预算")
    assert "基于已有信息的结论" in content["text"]


# ---------------------------------------------------------------------------
# 人工审批（HITL）：write/danger 级工具挂起等用户决定
# ---------------------------------------------------------------------------

async def _collect_with_approval(agen, approve: bool):
    """消费 agent_loop；遇到 approval_required 事件立刻按 approve 决议。"""
    events = []
    async for e in agen:
        events.append(e)
        if e["type"] == "approval_required":
            loop.resolve_approval(e["approval_id"], approve)
    return events


def test_write_tool_requires_approval_and_executes_on_approve(wire, monkeypatch):
    """write 级工具：先发 approval_required 挂起，批准后才执行。"""
    llm, tools = wire
    monkeypatch.setattr(loop, "tool_level", lambda name: "write")
    llm.script = [
        _resp(tool_calls=[_tc("w1", "danger_tool", '{"x": 1}')]),
        _resp(content="done"),
    ]

    events = asyncio.run(_collect_with_approval(loop.agent_loop("q"), approve=True))

    types = _types(events)
    assert "approval_required" in types
    req = next(e for e in events if e["type"] == "approval_required")
    assert req["name"] == "danger_tool" and req["level"] == "write"
    assert req["args"] == {"x": 1}
    # 批准后工具真的执行了
    assert tools.calls == [{"name": "danger_tool", "args": {"x": 1}}]
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["result"] == "[ok] danger_tool"


def test_rejected_tool_not_executed_becomes_observation(wire, monkeypatch):
    """拒绝后：工具不执行，错误 observation 喂回模型，循环继续。"""
    llm, tools = wire
    monkeypatch.setattr(loop, "tool_level", lambda name: "danger")
    llm.script = [
        _resp(tool_calls=[_tc("d1", "rm_tool", "{}")]),
        _resp(content="用户拒绝了，我改用只读方式"),
    ]

    events = asyncio.run(_collect_with_approval(loop.agent_loop("q"), approve=False))

    assert tools.calls == []   # 没执行
    result = next(e for e in events if e["type"] == "tool_result")
    assert "未获用户批准" in result["result"]
    # 拒绝是 observation 不是中断——模型收到了错误文本并给出结论
    assert events[-1]["type"] == "content"


def test_read_tool_never_asks_approval(wire):
    """read 级工具完全不进审批分支（wire fixture 默认 read）。"""
    llm, _ = wire
    llm.script = [
        _resp(tool_calls=[_tc("r1", "search_code", "{}")]),
        _resp(content="ok"),
    ]

    events = _run(loop.agent_loop("q"))

    assert "approval_required" not in _types(events)


def test_resolve_approval_unknown_id_returns_false():
    """重复/不存在的 approval_id：幂等返回 False，不崩。"""
    assert loop.resolve_approval("nope", True) is False


def test_draft_ticket_full_flow(wire, monkeypatch, tmp_path):
    """集成：真实 draft_ticket（write 级）走完整审批流——

    approval_required 事件 → 用户批准 → 真实执行 → 工单落盘 → 结果回填模型。
    wire 默认把 call_tool/tool_level 全 mock 掉，这里指回真实 registry。
    """
    from services.agent import registry as real_registry
    from services.agent import tools as real_tools

    llm, _ = wire
    monkeypatch.setattr(loop, "call_tool", real_registry.call_tool)
    monkeypatch.setattr(loop, "tool_level", real_registry.tool_level)
    monkeypatch.setattr(real_tools, "TICKET_DIR", tmp_path / "tickets")

    llm.script = [
        _resp(tool_calls=[_tc(
            "t1", "draft_ticket",
            '{"title": "订单 503", "severity": "high", '
            '"root_cause": "POOL_MAX=1 (src/db.py:10)", "suggested_fix": "回滚 POOL_MAX"}',
        )]),
        _resp(content="工单已建"),
    ]

    events = asyncio.run(_collect_with_approval(loop.agent_loop("q"), approve=True))

    req = next(e for e in events if e["type"] == "approval_required")
    assert req["name"] == "draft_ticket" and req["level"] == "write"
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["result"].startswith("[工单已创建]")
    # 真实副作用：文件落盘且内容含入参
    files = list((tmp_path / "tickets").glob("*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    assert "严重程度: high" in body and "POOL_MAX=1" in body


def test_draft_ticket_rejected_no_file(wire, monkeypatch, tmp_path):
    """拒绝后 draft_ticket 不执行：不落盘，错误 observation 回传。"""
    from services.agent import registry as real_registry
    from services.agent import tools as real_tools

    llm, _ = wire
    monkeypatch.setattr(loop, "call_tool", real_registry.call_tool)
    monkeypatch.setattr(loop, "tool_level", real_registry.tool_level)
    monkeypatch.setattr(real_tools, "TICKET_DIR", tmp_path / "tickets")

    llm.script = [
        _resp(tool_calls=[_tc(
            "t1", "draft_ticket",
            '{"title": "t", "severity": "low", "root_cause": "rc", "suggested_fix": "f"}',
        )]),
        _resp(content="好的，文字说明"),
    ]

    events = asyncio.run(_collect_with_approval(loop.agent_loop("q"), approve=False))

    assert not (tmp_path / "tickets").exists()   # 没有任何副作用
    result = next(e for e in events if e["type"] == "tool_result")
    assert "未获用户批准" in result["result"]
