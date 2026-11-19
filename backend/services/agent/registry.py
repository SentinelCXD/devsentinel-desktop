"""工具注册表：把 Python 函数变成"模型可调用的工具"。

三件套：
1. 参数模型（Pydantic）：既是入参校验器，又自动生成 JSON Schema
2. TOOL_REGISTRY：工具名 → {函数, 参数模型, 描述}
3. call_tool：按名字分发调用，把"模型给的 JSON"翻译成 Python 调用

模型侧只看到 tool_schemas() 输出的说明书；执行侧只调 call_tool()。
新增工具 = 写一个函数 + 一个 Args 模型 + 注册一行。
"""
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

import re

from .skills import load_skill
from .tools import read_log, search_code, git_log, git_show, draft_ticket, tail_errors

# ---------- 参数模型：描述就是给模型看的工具说明 ----------

class ReadLogArgs(BaseModel):
    """read_log 的入参：按服务/时间窗/关键词过滤日志。"""
    service: str = Field(
        default="",
        description=(
            "目标服务名，对应日志文件名（如 'order-service' 对应 order-service.log）。"
            "【重要限制】一旦填入就只搜索这一个文件，其他日志完全不搜索；"
            "只有用户明确指定日志所属服务时才填，故障发生在某个接口不等于日志只在该文件中。"
            "无法确定、需要全局排查或未找到证据时，留空搜索全部日志文件。"
        )
    )


class TailErrorsArgs(BaseModel):
    """tail_errors 无参数：扫描日志目录内全部服务日志。"""
    pass
    keywords: list[str] = Field(
        default_factory=list,
        description=(
            "日志关键词（如 timeout、total=、ERROR），采用不区分大小写的子串匹配，不支持正则。"
            "多个关键词之间是 OR 关系，无需同时出现；按命中的不同关键词数选择最佳文件。"
            "建议传 1 至 4 个核心英文术语或错误片段。留空返回时间窗内全部行。"
        )
    )
    start: str = Field(
        default="",
        description="时间窗起点，如 '15:00' 或 '2026-11-08 15:00:11'。留空不限，没有时间戳的行不会被过滤掉",
    )
    end: str = Field(
        default="",
        description="时间窗终点，格式同 start。留空不限，没有时间戳的行不会被过滤掉",
    )
    max_lines: int = Field(
        default=50,
        description=(
            "单次最多返回的行数限制（默认 50 行），用于防止超长输出爆上下文。"
            "【防截断机制】若返回结果带有截断提示且未看全，可适当调大此值；"
            "若内容过于冗杂，应考虑收窄检索范围或更换精准工具，切勿盲目无限放大。"
        )
    )


class SearchCodeArgs(BaseModel):
    """search_code 的入参：在服务源码里搜字符串。"""
    query: str = Field(
        description=(
            "代码搜索词：支持函数名、变量名、常量名或核心代码片段（如 POOL_MAX、dec_stock、timeout）。"
            "【匹配规则】采用纯子串匹配（不支持正则表达式），不区分大小写。"
            "【排查建议】若首次搜索无结果，切勿盲目加长表达式，应主动**缩短或精简关键词**（例如将复杂的长函数名拆为核心名词或动作词）后再次尝试。"
        )
    )
    max_lines: int = Field(
        default=30,
        description=(
            "单次最多返回的代码行数（默认 30 行）。"
            "若结果带有 '已截断' 标记且关键代码未看全，可适当调大；"
            "若结果噪音太大，应精简 query 缩小搜索范围，不要盲目放大此参数。"
        )
    )


class GitLogArgs(BaseModel):
    """git_log 的入参：看最近提交历史。"""
    max_count: int = Field(
        default=10,
        description=(
            "最多查看的提交历史条数限制（默认 10 条，上限为 30 条）。"
            "【防超载机制】用于控制 git_log 的返回广度，即使传入大于 30 的值也强制截断为 30 条。"
            "若 10 条内未找到语义相关的嫌疑提交，可适当调大此值；若 30 条全看完了仍无头绪，说明故障与近期代码变更无关，应立刻切换至日志或代码搜索工具。"
        )
    )

class GitShowArgs(BaseModel):
    """git_show 的入参：看某次提交的具体改动与元信息。"""

    commit: str = Field(
        description=(
            "目标 commit 的 hash 值（必须是 4 到 40 位的十六进制字符串，如 783f100），"
            "【严禁事项】绝不支持 HEAD~1、分支名或相对路径等写法，必须从 git_log 的返回结果中获取真实 hash。"
            "【核心原则】切忌盲目只看 git_log 的第一条！当某条历史提交的 Message 与故障现象语义相关时，"
            "必须传入该条提交的 hash 来查看其具体的改动明细。"
        )
    )

    max_lines: int = Field(
        default=80,
        description=(
            "单次最多返回的 diff 代码行数限制（默认 80 行）。"
            "【防截断机制】若返回结果包含 '已截断' 提示且关键改动未看全，可适当调大此值；"
            "若单次改动范围过大导致噪音淹没核心逻辑，建议结合 git_log 寻找更小的单点提交，切勿盲目无限放大此参数。"
        )
    )

    @field_validator("commit")
    @classmethod
    def validate_commit_hash(cls, v: str) -> str:
        # 校验是否为 4~40 位的十六进制字符
        if not re.fullmatch(r"^[0-9a-fA-F]{4,40}$", v):
            raise ValueError(
                f"非法的 commit 格式: '{v}'。"
                "git_show 仅接受 4~40 位十六进制 commit hash（严禁使用 HEAD~1 等相对写法），请先通过 git_log 获取合法的 hash。"
            )
        return v


class DraftTicketArgs(BaseModel):
    """draft_ticket 的入参：把诊断结论整理成故障工单。"""
    title: str = Field(
        description=(
            "工单标题：一句话概括故障（如 '订单服务 503：连接池耗尽'）。"
            "标题会作为文件名的一部分，请控制在 30 字以内并避免特殊字符。"
        )
    )
    severity: Literal["low", "medium", "high", "critical"] = Field(
        description=(
            "严重程度：low（轻微）/ medium（一般）/ high（严重）/ critical（致命）。"
            "依据故障影响面和证据的置信度综合判断。"
        )
    )
    root_cause: str = Field(
        description=(
            "根因分析正文：必须引用具体证据（日志行、文件:行号、commit hash），"
            "不要凭空猜测。这是工单的核心内容。"
        )
    )
    suggested_fix: str = Field(
        description="修复建议：具体可执行的改法（改哪个文件/配置/代码），可含验证步骤。"
    )


class LoadSkillArgs(BaseModel):
    """load_skill 的入参：取一本排查手册的全文。"""
    name: str = Field(
        description=(
            "手册名称，必须从系统提示的「可用排查手册」清单中原样选择"
            "（如 'mysql-troubleshooting'），不支持模糊匹配。"
            "清单里没有与故障相关的手册时不要调用本工具。"
        )
    )


# ---------- 注册表 ----------

TOOL_REGISTRY: dict[str, dict] = {
    "tail_errors": {
        "level": "read",
        "func": tail_errors,
        "args_model": TailErrorsArgs,
        "description": (
            "汇总靶场所有服务日志中的错误行数，并返回每个服务最近三条错误示例及文件行号。"
            "【使用时机】需要先比较各服务错误规模或全局查看近期错误时调用；无日志文件时改用 read_log 检查日志目录/范围。"
        )
    },
    "read_log": {
        "level": "read",
        "func": read_log,
        "args_model": ReadLogArgs,
        "description": (
            "按服务名、时间窗和关键词过滤业务日志，返回命中片段及来源文件标注。"
            "【输出限制】每次仅返回命中不同关键词数量最多的 1 个日志文件，避免上下文过载。"
            "【未命中或无有效证据时的排查调整策略】请按以下顺序换方式再查："
            "1) 放宽范围：若指定了 service，将其置空改为全局跨服务搜索；"
            "2) 精简/替换关键词：因底层采用不区分大小写的纯子串 OR 匹配（不支持正则），切勿堆砌长词，应缩短或精简为 1~2 个更通用的英文错误词（如 ERROR、timeout、fail）；"
            "3) 扩大时间窗：放宽 start 或 end 时间限制；"
            "4) 切换工具：若上述调整后仍无果，停止死磕日志，转而调用代码检索或 Git 变更工具（git_log）排查。"
        )
    },
    "search_code": {
        "level": "read",
        "func": search_code,
        "args_model": SearchCodeArgs,
        "description": (
            "在服务源码的 src/ 目录下搜索指定的字符串或代码片段（仅匹配 .py 文件），"
            "返回 '文件:行号 + 上下文片段' 样式的真实输出。"
            "【使用时机】当你通过日志或 Git 提交历史锁定了可疑模块，需要定位具体代码根因时使用。"
            "【未命中降级策略】若未搜索到目标代码：1) 检查关键词是否拼写过细或带了动态变量；"
            "2) 尝试精简或拆分搜索词（如将长函数名拆为核心关键字）；"
            "3) 若代码中确实没有，请转而检查配置文件或通过 git_log 排查近期变更。"
        )
    },
    "git_log": {
        "level": "read",
        "func": git_log,
        "args_model": GitLogArgs,
        "description": (
            "查看靶场仓库最近的提交历史（返回 hash + 提交信息）。"
            "【触发时机】当日志与源码检索无法解释故障、怀疑是最近代码变更引入时使用。"
            "【后续与筛选策略】获取历史后，切忌只看最显眼的第一条；应仔细比对 Commit Message，若发现与故障现象高度相关的提交（如涉及重构、配置或底层调整），下一步必须立即调用 git_show 查看其详细改动明细。"
        )
    },
    "git_show": {
        "level": "read",
        "func": git_show,
        "args_model": GitShowArgs,
        "description": (
            "查看某次提交的完整 diff（代码变更明细）。"
            "【使用时机】必须在通过 git_log 拿到提交历史后配合使用。"
            "【核心原则】切忌盲目只看最显眼的第一条提交！当某条提交的 Message（如涉及重构、配置调整或底层逻辑变更）与当前故障现象在语义上相关时，"
            "必须立即调用此工具查看其具体的改动行，锁定嫌疑代码。"
        )
    },
    "load_skill": {
        "level": "read",
        "func": load_skill,
        "args_model": LoadSkillArgs,
        "description": (
            "获取指定排查手册的全文。系统提示里只列了手册名称和一句话描述，"
            "需要系统性排查步骤/命令参考时用本工具加载对应手册。"
            "【使用时机】故障症状与某本手册的描述相关（如数据库连接池、K8s Pod 异常），"
            "或常规排查卡壳需要方法论时。"
            "【注意】手册是参考资料，最终结论仍必须引用日志/代码证据，不能只凭手册下判断。"
        )
    },
    "draft_ticket": {
        "level": "write",   # 写文件副作用：执行前必须人工审批（HITL）
        "func": draft_ticket,
        "args_model": DraftTicketArgs,
        "description": (
            "起草故障工单：把诊断结论整理成 markdown 工单并保存到 tickets/ 目录。"
            "【使用时机】根因分析完成、证据充分后调用，作为排查的最终产出。"
            "【注意】此工具为 write 级，执行前需要用户审批；根因分析必须引用具体证据，"
            "证据不足时先继续排查（read_log / search_code / git_log），不要提前开单。"
        )
    },
}


def tool_schemas() -> list[dict]:
    """生成 OpenAI tools 参数格式的 schema 列表，直接喂给 chat.completions 的 tools=。"""
    schemas = []
    for name, spec in TOOL_REGISTRY.items():
        params = spec["args_model"].model_json_schema()
        # model_json_schema 会带 $defs/title 等，OpenAI 只要 properties/required
        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": params,
            },
        })
    return schemas


def call_tool(name: str, arguments: dict) -> str:
    """按名字分发调用工具。

    返回值永远是字符串——成功是结果，失败是给模型的错误提示。
    不抛异常：错误要喂回模型让它修正参数重试，异常会中断 Agent 循环。
    """
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        return f"[错误] 未知工具: {name}。可用工具: {', '.join(TOOL_REGISTRY)}"

    if not isinstance(arguments, dict):
        return f"[错误] arguments 必须是 JSON 对象，收到: {type(arguments).__name__}"

    try:
        args = spec["args_model"].model_validate(arguments)
    except ValidationError as e:
        # 把校验错误压缩成模型能看懂的一行提示
        problems = "; ".join(
            f"{'.'.join(str(l) for l in err['loc'])}: {err['msg']}" for err in e.errors()
        )
        return f"[错误] 参数校验失败: {problems}。请修正后重试"

    try:
        return spec["func"](**args.model_dump())
    except Exception as e:
        return f"[错误] 工具执行异常: {type(e).__name__}: {e}"


def tool_level(name: str) -> str:
    """本地工具的权限级别：read / write / danger。

    未知工具返回 'write'（fail-closed：不确定就当有副作用，走审批）。
    """
    spec = TOOL_REGISTRY.get(name)
    return spec.get("level", "write") if spec else "write"
