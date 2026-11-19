"""Skill 机制：排查手册的渐进式加载。

设计目标（省上下文）：
- system prompt 只放 skill_index()——每本手册一行"名称: 描述"；
- 模型判断与当前故障相关时，调用 load_skill 工具，全文作为工具结果
  注入对话。手册正文永远不进 system prompt——这是本机制的验收点。

手册是"内容资产"不是代码：放 backend/skills/*.md（与 services/ 分离，
AI 起草、人工审阅，改手册不用改代码、不用重启服务）。

文件格式（frontmatter 两个字段各占一行，行级解析即可，不引 PyYAML）：
    ---
    name: mysql-troubleshooting
    description: MySQL 连接池耗尽、慢查询、死锁排查
    ---
    # 正文（给模型看的手册内容）
"""
from dataclasses import dataclass
from pathlib import Path

from .tools import _truncate

SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "skills"

MAX_LINES = 250   # 手册全文注入上限（行数），与 _truncate 的字符数兜底共用同一套截断


@dataclass(frozen=True)
class Skill:
    """一本手册：frontmatter 元数据 + 剥掉头部的正文。"""
    name: str
    description: str
    body: str
    path: Path


def _parse_skill(path: Path) -> Skill | None:
    """解析单本手册；格式不对返回 None（跳过该文件，不炸整个 Agent）。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if not lines or lines[0].strip() != "---":
        return None

    meta: dict[str, str] = {}
    end = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
        key, _, value = line.partition(":")
        if key.strip() in ("name", "description") and value.strip():
            meta[key.strip()] = value.strip()
    if "name" not in meta or "description" not in meta or end < 0:
        return None

    return Skill(
        name=meta["name"],
        description=meta["description"],
        body="\n".join(lines[end + 1:]).strip(),
        path=path,
    )


def list_skills() -> list[Skill]:
    """扫描手册目录，返回全部合法手册（按名称排序）。

    目录不存在 → 空列表：手册是增强不是依赖，缺了 Agent 依旧可用
    （与工具权限的 fail-closed 相反，这里 fail-open——少知识无风险）。
    不缓存：每次读盘，改手册即时生效。
    """
    if not SKILLS_DIR.is_dir():
        return []
    skills = (s for s in (_parse_skill(p) for p in sorted(SKILLS_DIR.glob("*.md"))) if s)
    return sorted(skills, key=lambda s: s.name)


def skill_index() -> str:
    """system prompt 用的手册索引：只有名称和描述，绝不含正文。"""
    skills = list_skills()
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description}" for s in skills]
    return (
        "可用排查手册（Skill）——索引只有名称和一句话描述，"
        "全文需调用 load_skill 工具按需获取；症状与某本手册相关时先加载再排查：\n"
        + "\n".join(lines)
    )


def load_skill(name: str) -> str:
    """load_skill 工具的函数体：返回指定手册全文（剥掉 frontmatter）。

    契约与 tools.py 一致：返回字符串、永不抛异常，错误用 [错误] 前缀
    喂回模型自我修正（打错名字时把可用清单还给它）。
    name 只做字典匹配、不拼文件路径，无路径穿越风险。
    """
    for s in list_skills():
        if s.name == name:
            return _truncate(s.body.splitlines(), MAX_LINES)
    available = ", ".join(s.name for s in list_skills()) or "（无）"
    return f"[错误] 未知手册: {name}。可用手册: {available}。请从系统提示的清单中原样选择"
