import asyncio
import json
import uuid
from typing import Any, AsyncIterator

from services.llm.client import client, provider
from services.mcp import manager as mcp_manager
from services.mcp.schema import MCP_PREFIX
from .registry import tool_schemas, call_tool, tool_level
from .skills import skill_index

BASE_PROMPT = (
    "你是 DevSentinel 故障排查 Agent。你可以调用工具排查问题："
    "read_log 查日志证据，search_code 定位代码，git_log/git_show 查近期变更。"
    "证据足够时给出根因分析和修复建议；证据不足时继续调用工具。"
    "根因明确后，调用 draft_ticket 起草故障工单（该工具需要用户审批，被拒绝时"
    "直接以文字形式给出根因分析和修复建议即可）。"
    "结论要引用具体证据（日志行、文件:行号），不要凭空猜测。"
)


def build_system_prompt() -> str:
    """基础指令 + 手册索引（Skill 渐进式加载）。

    索引只含名称和描述（skill_index 的约定），全文由模型调 load_skill
    按需注入——手册正文不进 system prompt，省上下文。
    每次请求重建：改手册文件不用重启服务。
    """
    index = skill_index()
    return f"{BASE_PROMPT}\n{index}" if index else BASE_PROMPT

# 挂起中的人工审批：approval_id → Future[bool]。
# agent_loop 遇到 write/danger 级工具时 await 这个 Future 挂起，
# /api/agent/approve 端点调 resolve_approval 唤醒——SSE 流不结束，
# 前端在时间线上渲染"待审批"节点，用户点批准/拒绝后循环继续。
_pending_approvals: dict[str, asyncio.Future] = {}

APPROVAL_TIMEOUT = 300  # 秒；超时视为拒绝，防止用户不操作导致 SSE 永久挂起


def resolve_approval(approval_id: str, approved: bool) -> bool:
    """供 /api/agent/approve 调用：把用户的决定写进 Future 唤醒 agent_loop。"""
    fut = _pending_approvals.pop(approval_id, None)
    if fut is None or fut.done():
        return False
    fut.set_result(approved)
    return True


def create_approval(name: str, args: dict, level: str, tool_call_id: str | None = None):
    """注册一个待审批项：返回 (approval_required 事件, 等决议的 Future)。

    agent_loop 和联调注入流（server.py mock_approval）共用同一套
    挂起/唤醒机制——/api/agent/approve 对两者同样生效。
    """
    approval_id = uuid.uuid4().hex[:12]
    fut = asyncio.get_running_loop().create_future()
    _pending_approvals[approval_id] = fut
    evt = {
        "type": "approval_required",
        "approval_id": approval_id,
        "tool_call_id": tool_call_id,   # 前端据此把"待审批"标记配对到时间线上的工具
        "name": name,
        "args": args,
        "level": level,
    }
    return evt, fut


async def await_approval(fut: asyncio.Future, approval_id: str) -> bool:
    """挂起等用户决议；超时按拒绝处理（防止 SSE 永久挂起）。

    finally 幂等清理：approve 成功路径已在 resolve_approval 里 pop，
    超时路径没人清，这里兜底，防 _pending_approvals 泄漏。
    """
    try:
        return await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
    except asyncio.TimeoutError:
        return False
    finally:
        _pending_approvals.pop(approval_id, None)


def _call_signature(name: str, arguments: str) -> tuple[str, Any]:
    """生成去重签名 + 解析后的参数。

    返回 (sig, args)：args 为 None 表示 arguments 不是合法 JSON。
    sig 必须对"语义相同"的调用稳定：先解析再用 sort_keys 重序列化，
    消除键序/空白等文本差异——否则同一调用换种写法就能绕过去重。
    无法解析时退回原始字符串（该调用反正会以报错 observation 结束）。
    """
    try:
        args = json.loads(arguments)
    except json.JSONDecodeError:
        return f"{name}:{arguments}", None
    return f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}", args


async def agent_loop(question: str, max_steps: int = 10) -> AsyncIterator[dict]:
    """手写 Agent 循环：模型决定调工具 → 执行 → 回填 → 再问，直到给结论。

    通过 yield 推送事件：
      step        每轮开始的进度标记
      tool_call   模型决定调用的工具 + 参数
      approval_required  write/danger 级工具等待人工审批（挂起循环）
      tool_result 工具执行结果
      content     最终答案（模型不再调工具）
      error       异常
    """
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": question},
    ]
    fail_count: dict[str, int] = {}   # 工具失败次数统计
    seen: set[str] = set()  # 检测重复调用
    total_tokens = 0
    TOKEN_BUDGET = 30000      # 单次请求预算

    for step in range(max_steps):          # 步数上限：防止死循环/烧钱
        yield {
            "type": "step",
            "name": f"round_{step + 1}",
            "label": f"第 {step + 1} 轮思考",
        }
        try:
            resp = await client.chat.completions.create(
                model=provider.model,
                messages=messages,
                # 本地 registry 工具 + 已连接 MCP server 的远程工具（未连接时为空）
                tools=tool_schemas() + mcp_manager.schemas(),
                temperature=0.3,
            )
            total_tokens += resp.usage.total_tokens if resp.usage else 0  # 每轮累计真实消耗
            if total_tokens > TOKEN_BUDGET:
                messages.append({
                    "role": "system",
                    "content": "Token 使用已超过预算，请基于已有信息直接给结论，不要再调工具",
                })
                # 不带 tools 的最后一次调用：强制模型收尾而不是继续调工具
                resp = await client.chat.completions.create(
                    model=provider.model, messages=messages, temperature=0.3)
                yield {
                    "type": "content",
                    "text": f"[已达 token 预算 {TOKEN_BUDGET}]\n\n" + (resp.choices[0].message.content or ""),
                }
                return

        except Exception as e:
            yield {
                "type": "error",
                "code": "llm_call_failed",
                "message": str(e),
                "retryable": step + 1 < max_steps,
            }
            return

        msg = resp.choices[0].message

        # 模型不调工具了 → content 就是最终答案
        if not msg.tool_calls:
            yield {"type": "content", "text": msg.content or ""}
            return

        # 如果有 tool_calls 且有 content，则先输出思考
        # 去除首尾空白，避免空行被误认为思考内容
        thought = (msg.content or "").strip()
        if msg.tool_calls and thought:
            yield {"type": "thought", "text": thought}

        # -------------------------------------------------------------
        # 阶段 1：串行准备——发事件、去重、解析参数、构造并发任务
        # 去重必须在这里完成：等结果回来再标记，同轮的重复调用就都执行了
        # -------------------------------------------------------------
        messages.append(msg)   # 模型原话回填；协议要求每个 tool_call 后跟一条 tool 消息
        prep: list[dict] = []  # 每个 tc 一项：execute 带 coro，shortcut 带预计算 result
        for tc in msg.tool_calls:
            # 先把所有 tool_call 事件发出去：前端时间线上它们会同时进入"执行中"
            yield {
                "type": "tool_call",
                "name": tc.function.name,
                "args": tc.function.arguments,
                "id": tc.id,
            }

            sig, args = _call_signature(tc.function.name, tc.function.arguments)
            if sig in seen:
                # 重复调用：不执行、不发协程，直接给模型提示
                # continue 必不可少——否则会落入下方执行分支，同一个 id 回填两条结果
                prep.append({
                    "tc": tc,
                    "result": "[提示] 该工具已用相同参数调用过，请换策略或直接给结论",
                })
                continue
            seen.add(sig)

            if args is None:
                prep.append({
                    "tc": tc,
                    "result": f"[错误] 工具参数不是合法 JSON: {tc.function.arguments}",
                })
                continue

            name = tc.function.name
            # 权限分级（HITL）：read 直接执行；write/danger 先请求人工审批。
            # MCP 工具的级别来自 server 的 annotations（client.py connect 时提取），
            # 本地工具来自 TOOL_REGISTRY 的 level 字段；查不到按 write 处理（fail-closed）。
            level = (
                mcp_manager.level(name)
                if name.startswith(MCP_PREFIX)
                else tool_level(name)
            )
            if level != "read":
                evt, fut = create_approval(name, args, level, tool_call_id=tc.id)
                yield evt
                # 挂起整个 agent_loop：SSE 流保持打开，等前端 approve 端点唤醒
                approved = await await_approval(fut, evt["approval_id"])
                if not approved:
                    # 拒绝/超时不是中断——和错误同理，是 observation 喂回模型
                    prep.append({
                        "tc": tc,
                        "result": (
                            f"[错误] 工具 {name}（{level} 级）未获用户批准，已跳过执行。"
                            "请改用只读工具排查，或基于已有信息直接给结论。"
                        ),
                    })
                    continue

            # 路由：mcp__ 前缀走远程 MCP server（本身就是协程）；
            # 本地同步函数必须 to_thread 丢进线程池，否则 gather 会退化成串行
            # 如果是 MCP 工具，走远程调用
            if name.startswith(MCP_PREFIX):
                coro = mcp_manager.call(name, args)
            # 否则走本地工具，丢进线程池并发执行
            else:
                coro = asyncio.to_thread(call_tool, name, args)
            prep.append({"tc": tc, "coro": coro})

        # -------------------------------------------------------------
        # 阶段 2：并发执行
        # return_exceptions=True：单个失败作为返回值回来，不炸整批（"单个失败不影响其他"）
        # gather 的返回顺序 = 传入顺序，不是完成顺序——正好满足按 tool_calls 原序回填
        # -------------------------------------------------------------
        results = await asyncio.gather(
            *(p["coro"] for p in prep if "coro" in p),
            return_exceptions=True,
        )
        it = iter(results)
        for p in prep:
            if "coro" not in p:
                continue
            r = next(it)
            # 异常转成 observation 字符串喂回模型，而不是中断循环（与 call_tool 同一哲学）
            p["result"] = f"[错误] 工具执行异常: {r}" if isinstance(r, BaseException) else r

        # -------------------------------------------------------------
        # 阶段 3：按 tool_calls 原顺序收尾
        # 注意：这段必须留在 for step 循环内（缩进一层）——挪出去会导致
        # tool 消息没回填，下一轮 LLM 请求因 tool_calls 悬空直接 400
        # -------------------------------------------------------------
        for p in prep:
            tc = p["tc"]
            result = p["result"]

            # 连续失败计数：错误也是 Observation，但失败多次要提示模型换思路
            if result.startswith("[错误]"):
                fail_count[tc.function.name] = fail_count.get(tc.function.name, 0) + 1
                if fail_count[tc.function.name] >= 2:
                    result += " 已多次失败，建议换其他工具或直接说明无法获取"
            else:
                fail_count[tc.function.name] = 0

            # 两份内容有意不同：前端事件截断 2000 防爆屏；
            # 回填给模型的用完整结果（工具层 _truncate 已有 4000 字符兜底）
            yield {
                "type": "tool_result",
                "name": tc.function.name,
                "result": result[:2000],
                "id": tc.id,
            }
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,   # 模型靠这个 id 把结果和调用配对
                "content": result,
            })
        # 回到循环顶部：模型看到工具结果，再决定下一步

    yield {
        "type": "error",
        "code": "max_steps",
        "message": f"达到最大步数 {max_steps}，请人工介入",
        "retryable": False,
    }
