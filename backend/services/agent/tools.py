"""工具函数：纯程序实现，不调用模型。

这是工作流和 Agent 共用的"手"——查日志、搜代码这类确定的活。
后面给 Agent 用时，这些函数会包上 JSON Schema 变成 tools。
"""
from pathlib import Path

# 故障靶场目录
LAB_DIR = Path(__file__).parent.parent.parent / "fault_lab"
LOG_DIR = LAB_DIR / "logs"
CODE_DIR = LAB_DIR / "order-service" / "src"   # 靶场服务源码
CONFIG_DIR = LAB_DIR / "order-service"         # 靶场服务配置根目录

MAX_LINES = 50       # 输出截断：日志可能几千行，全塞给模型会爆上下文
MAX_CHARS = 4000     # 字符数兜底：行数没超但单行超长（如整段 JSON）时生效
CONTEXT_LINES = 2    # 命中行前后带几行上下文


def _truncate(lines: list[str], max_lines: int) -> str:
    """统一截断：行数 + 字符数双上限。所有给模型的返回都走这里，格式一致。"""
    text = "\n".join(lines[:max_lines])
    if len(lines) > max_lines:
        text += f"\n... [已截断，共 {len(lines)} 行，只显示前 {max_lines} 行]"
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + f"\n... [已截断，共 {len(text)} 字符，只显示前 {MAX_CHARS} 字符]"
    return text


def _meta(name: str, score: int, kw_count: int, start: str, end: str, total: int) -> str:
    """统一的结果头部：来源 + 可选时间窗 + 命中情况。read_log / grep_logs 共用。"""
    parts = [f"来源: {name}"]
    if start or end:
        parts.append(f"时间窗: {start or '-∞'}~{end or '+∞'}")
    if kw_count:
        parts.append(f"命中关键词 {score}/{kw_count}")
    elif total:
        parts.append(f"共 {total} 行")
    return "[" + " | ".join(parts) + "]"


def _search(lines: list[str], kw: list[str], max_lines: int) -> tuple[str, int] | None:
    """在一组日志行里搜关键词。

    返回 (命中片段, 命中的不同关键词数)；无命中返回 None。
    分数 = 不同关键词数（不是命中行数）——防止单个泛词刷屏抢占结果。
    """
    hit: set[int] = set()
    matched: set[str] = set()
    for i, line in enumerate(lines):
        low = line.lower()
        line_kws = [k for k in kw if k in low]
        if line_kws:
            matched.update(line_kws)
            for j in range(max(0, i - CONTEXT_LINES), min(len(lines), i + CONTEXT_LINES + 1)):
                hit.add(j)
    if not hit:
        return None
    ordered = sorted(hit)
    return _truncate([lines[i] for i in ordered], max_lines), len(matched)


def grep_logs(service: str, keywords: list[str], max_lines: int = MAX_LINES) -> str:
    """按服务名 + 关键词过滤日志。

    返回：得分最高的命中片段（截断后，标注来源文件和命中关键词数）。
    设计要点：
    - 遍历所有日志文件评分（命中关键词数），选分数最高的；
      service 文件优先做平局 tie-break。
    - 防止单个泛词（如 "order"）在无关文件里刷屏抢占结果。
    - 返回值是给模型看的，要做截断和格式化，不是原样转发。
    """
    kw = [k.lower() for k in keywords]
    primary = LOG_DIR / f"{service}.log"

    candidates = [primary] if primary.exists() else []
    candidates += [p for p in sorted(LOG_DIR.glob("*.log")) if p != primary]
    if not candidates:
        return f"[无日志文件] {LOG_DIR}"

    best: tuple[str, int, int] | None = None  # (片段, 分数, 是否主文件)
    for i, path in enumerate(candidates):
        lines = path.read_text(encoding="utf-8").splitlines()
        result = _search(lines, kw, max_lines)
        if result is None:
            continue
        snippet, score = result
        is_primary = 1 if i == 0 else 0
        if best is None or (score, is_primary) > (best[1], best[2]):
            best = (f"{_meta(path.name, score, len(kw), '', '', 0)}\n{snippet}", score, is_primary)

    if best is None:
        return f"[日志中未命中关键词: {', '.join(keywords)}]"
    return best[0]

def _in_window(line: str, start: str, end: str) -> bool:
    """时间窗过滤：行前缀时间戳与 start/end 做字符串前缀比较。

    兼容 "15:00" 和 "2026-11-08 15:00" 两种粒度：
    短前缀按"尾部对齐"比（取行时间戳的最后 len(start) 个字符），
    长前缀按"头部对齐"比。不起作用的行（无时间戳前缀）默认放行。
    """
    ts = line[:19]  # "YYYY-MM-DD HH:MM:SS" 刚好 19 字符
    if not ts or not ts[0].isdigit():
        return True
    tod = ts[11:]  # "HH:MM:SS" 部分
    if start:
        # 短前缀（如 "15:00"）比时分秒；长前缀（含日期）比完整时间戳
        field = tod if len(start) <= 8 else ts
        if field[:len(start)] < start:
            return False
    if end:
        field = tod if len(end) <= 8 else ts
        if field[:len(end)] > end:
            return False
    return True


def read_log(service: str = "",
             keywords: list[str] = [],
             start: str = "",
             end: str = "",
             max_lines: int = MAX_LINES) -> str:
    """按 服务 → 时间窗 → 关键词 三层过滤读取日志。

    返回：命中片段（标注来源/时间窗/命中数），给模型做证据。
    跨文件搜索时附带其他命中文件列表——模型才知道还有哪些日志可查。
    所有参数可缺省：service 空 = 全目录搜索，keywords 空 = 时间窗内全部行。
    """
    # ① 服务过滤：选候选文件
    primary = LOG_DIR / f"{service}.log" if service else None
    if primary and primary.exists():
        files = [primary]
    elif service:
        return f"[无日志文件] {LOG_DIR / (service + '.log')}，可省略 service 搜全部"
    else:
        files = sorted(LOG_DIR.glob("*.log"))
    if not files:
        return f"[无日志文件] {LOG_DIR}"

    kw = [k.lower() for k in keywords]
    scored: list[dict] = []   # 每个文件的命中：name / score / primary / text

    for i, path in enumerate(files):
        lines = path.read_text(encoding="utf-8").splitlines()
        total = len(lines)

        # ② 时间过滤
        if start or end:
            lines = [l for l in lines if _in_window(l, start, end)]
            if not lines:
                continue

        # ③ 关键词过滤（空关键词 = 全量返回时间窗内行）
        if kw:
            result = _search(lines, kw, max_lines)
            if result is None:
                continue
            snippet, score = result
        else:
            snippet, score = _truncate(lines, max_lines), 0

        scored.append({
            "name": path.name,
            "score": score,
            "primary": 1 if i == 0 else 0,
            "text": f"{_meta(path.name, score, len(kw), start, end, total)}\n{snippet}",
        })

    if not scored:
        if kw:
            return f"[未命中] 关键词 {keywords} 在指定范围内无结果，可换关键词或扩大时间窗"
        return f"[无日志] 时间窗 {start}~{end} 内无日志行"

    best_i = max(range(len(scored)), key=lambda i: (scored[i]["score"], scored[i]["primary"]))
    out = scored[best_i]["text"]

    # 跨文件搜索时列出其他命中文件（按得分排序），模型可据此换查
    if kw and len(files) > 1:
        others = sorted(
            (r for i, r in enumerate(scored) if i != best_i and r["score"] > 0),
            key=lambda r: -r["score"],
        )
        if others:
            names = ", ".join(f"{r['name']} ({r['score']}/{len(kw)})" for r in others)
            out += f"\n[其他命中文件: {names}]"
    return out

CODE_CONTEXT_LINES = 3  # 代码命中行前后各带 3 行（比日志多，代码要上下文才能看懂）


def search_code(query: str, max_lines: int = 30) -> str:
    """在靶场服务源码里做子串搜索，返回 文件:行号 + 上下文片段。

    返回格式：
        src/db.py:14: POOL_MAX = 1
        （命中行带行号，前后各 CODE_CONTEXT_LINES 行上下文，文件间分隔）
    给 Agent 用：模型拿到"文件:行号"才能指认证据位置。
    """
    if not CODE_DIR.exists():
        return f"[无代码目录] {CODE_DIR}"

    q = query.lower()
    hits: list[str] = []
    hit_count = 0

    for path in sorted(CODE_DIR.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        rel = path.relative_to(CODE_DIR.parent)  # src/db.py

        # 收集命中行号（含上下文）
        hit_idx: set[int] = set()
        for i, line in enumerate(lines):
            if q in line.lower():
                hit_count += 1
                for j in range(max(0, i - CODE_CONTEXT_LINES), min(len(lines), i + CODE_CONTEXT_LINES + 1)):
                    hit_idx.add(j)

        if not hit_idx:
            continue

        # 输出 文件:行号: 内容，上下文块间用 -- 分隔
        ordered = sorted(hit_idx)
        block = [f"--- {rel} ---"]
        prev = -2
        for idx in ordered:
            if idx != prev + 1:
                block.append("  ...")
            block.append(f"{idx + 1:>4}: {lines[idx]}")
            prev = idx
        hits.append("\n".join(block))

    if not hits:
        return f"[代码中未命中: {query}] 可尝试更短的关键词或搜函数名/变量名"

    body = "\n\n".join(hits)
    body = _truncate(body.splitlines(), max_lines)
    return f"[代码搜索: {query} | {hit_count} 处命中]\n{body}"


def read_config(filename: str = "") -> str:
    """读取靶场服务配置文件元信息，避免将整份配置注入模型上下文。"""
    try:
        allowed = {".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".json", ".env"}
        if filename and (Path(filename).name != filename or filename in {".", ".."}):
            return "[错误] 只接受配置文件名，不接受目录或路径"
        if not CONFIG_DIR.is_dir():
            return f"[未命中] 配置目录不存在: {CONFIG_DIR}"
        candidates = sorted(
            p for p in CONFIG_DIR.rglob("*")
            if p.is_file() and p.suffix.lower() in allowed
        )
        if filename:
            candidates = [p for p in candidates if p.name == filename]
        if not candidates:
            return f"[未命中] 未找到配置文件{': ' + filename if filename else ''}"
        lines = []
        for path in candidates:
            stat = path.stat()
            count = len(path.read_text(encoding="utf-8").splitlines())
            lines.append(f"{path.name}: 大小 {stat.st_size} 字节，{count} 行，最后修改 {__import__('datetime').datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec='seconds')}")
        return _truncate(lines, MAX_LINES)
    except Exception as exc:
        return f"[错误] 读取配置元信息失败: {exc}"


REPO_DIR = LAB_DIR / "order-service"   # 靶场 git 仓库
GIT_CONTEXT_LINES = 2


def _git(args: list[str]) -> str:
    """在靶场仓库执行 git 命令，输出统一截断。不允许任意参数——白名单子命令。"""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_DIR), *args],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return "[错误] git 命令超时"
    if out.returncode != 0:
        return f"[错误] git {' '.join(args)}: {out.stderr.strip()[:300]}"
    return out.stdout.strip()


def git_log(max_count: int = 10) -> str:
    """查看最近提交历史：每条一行 hash + 信息。

    排查"最近改了什么"的第一步——对应 git log --oneline。
    """
    out = _git(["log", "--oneline", f"-{min(max_count, 30)}"])
    if out.startswith("[错误]"):
        return out
    return f"[git log 最近 {max_count} 条]\n{out or '(空仓库)'}"


def git_show(commit: str, max_lines: int = 80) -> str:
    """查看某次提交的具体改动：diff 内容。

    拿到 commit hash 后看"这次提交到底改了什么"——对应 git show。
    """
    # 只接受 7~40 位 hex，防止注入其他 git 参数
    if not commit or not all(c in "0123456789abcdef" for c in commit.lower()) or not (4 <= len(commit) <= 40):
        return f"[错误] 非法 commit: {commit!r}，应为 4~40 位 hex（如 783f100）"
    out = _git(["show", "--stat", commit])
    diff = _git(["show", "--format=%h %s%n%b", commit])
    if diff.startswith("[错误]"):
        return diff
    diff = _truncate(diff.splitlines(), max_lines)
    return f"[git show {commit}]\n{out}\n\n{diff}"


TICKET_DIR = LAB_DIR / "tickets"   # 工单草稿落盘目录（write 级工具的副作用）


def draft_ticket(title: str, severity: str, root_cause: str, suggested_fix: str) -> str:
    """起草故障工单：把根因结论整理成 markdown 落盘到 tickets/ 目录。

    write 级工具（有副作用：写文件），Agent 循环会先请求人工审批再执行。
    返回文件路径 + 工单全文，模型可把路径告诉用户。
    """
    from datetime import datetime
    import re as _re

    try:
        TICKET_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 标题转文件名：非安全字符替换成 _，限长防路径爆炸
        slug = _re.sub(r"[^\w-]+", "_", title).strip("_")[:30] or "ticket"
        path = TICKET_DIR / f"{ts}_{slug}.md"
        content = (
            f"# {title}\n\n"
            f"- 严重程度: {severity}\n"
            f"- 创建时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"- 来源: DevSentinel Agent\n\n"
            f"## 根因分析\n\n{root_cause}\n\n"
            f"## 修复建议\n\n{suggested_fix}\n"
        )
        path.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"[错误] 工单写入失败: {e}"
    return f"[工单已创建] {path.name}（目录 {TICKET_DIR}）\n\n{content}"
