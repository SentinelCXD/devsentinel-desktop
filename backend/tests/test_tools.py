"""tools.py 契约测试。

工具的约定（call_tool 依赖它）：永不抛异常，错误/未命中用约定前缀的
字符串返回——"[错误]"、"[未命中]"、"[无日志]"。模型靠这些提示自我纠错，
前缀断了整个纠错链路就断了。
"""
import pytest

from services.agent import tools
from services.agent.registry import TOOL_REGISTRY, tool_level


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    (d / "order-service.log").write_text(
        "2026-11-05 14:59:58 INFO  heartbeat ok\n"
        "2026-11-05 15:00:03 ERROR acquire connection timeout after 500ms\n"
        "2026-11-05 15:00:04 ERROR acquire connection timeout after 500ms\n"
        "no timestamp line mentioning timeout\n",
        encoding="utf-8",
    )
    (d / "other.log").write_text(
        "2026-11-05 15:00:05 INFO unrelated noise\n", encoding="utf-8"
    )
    monkeypatch.setattr(tools, "LOG_DIR", d)
    return d


@pytest.fixture
def code_dir(tmp_path, monkeypatch):
    d = tmp_path / "src"
    d.mkdir()
    (d / "db.py").write_text(
        "import sqlite3\n\nPOOL_MAX = 1\n\ndef acquire():\n    pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(tools, "CODE_DIR", d)
    return d


class TestInWindow:
    def test_short_prefix_uses_time_of_day(self):
        line = "2026-11-05 15:00:03 ERROR x"
        assert tools._in_window(line, "15:00", "")
        assert not tools._in_window(line, "15:01", "")

    def test_long_prefix_uses_full_timestamp(self):
        line = "2026-11-05 15:00:03 ERROR x"
        assert tools._in_window(line, "2026-11-05 14:00", "2026-11-05 16:00")
        assert not tools._in_window(line, "2026-11-06 00:00", "")

    def test_line_without_timestamp_passes(self):
        assert tools._in_window("no timestamp at all", "15:00", "16:00")


class TestSearch:
    def test_scores_distinct_keywords_not_lines(self):
        # "timeout" 命中 3 行，"error" 命中 1 行——分数是 2 不是 4
        lines = ["timeout", "timeout", "error timeout", "timeout"]
        result = tools._search(lines, ["timeout", "error", "miss"], 50)
        assert result is not None
        _, score = result
        assert score == 2

    def test_no_hit_returns_none(self):
        assert tools._search(["a", "b"], ["zzz"], 50) is None

    def test_truncation_marker(self):
        lines = ["hit"] * 100
        snippet, _ = tools._search(lines, ["hit"], 10)
        assert "[已截断" in snippet
        assert snippet.count("hit") == 10


class TestReadLog:
    def test_keyword_hit_marks_source(self, log_dir):
        out = tools.read_log(service="order-service", keywords=["timeout"])
        assert "[来源: order-service.log" in out
        assert "timeout" in out

    def test_unknown_service_returns_hint(self, log_dir):
        out = tools.read_log(service="no-such-svc", keywords=["x"])
        assert out.startswith("[无日志文件]")
        assert "可省略 service" in out

    def test_keyword_miss_returns_hint(self, log_dir):
        out = tools.read_log(keywords=["zzz-no-hit"])
        assert out.startswith("[未命中]")

    def test_time_window_filters_lines(self, log_dir):
        out = tools.read_log(start="15:00", end="15:00")
        assert "15:00:03" in out
        assert "14:59:58" not in out

    def test_empty_keywords_returns_window_lines(self, log_dir):
        out = tools.read_log(service="order-service")
        assert "共 4 行" in out
        # 无时间戳行默认放行
        assert "no timestamp" in out

    def test_contract_never_raises(self, log_dir):
        out = tools.read_log(service="missing", keywords=["x"], start="99:99")
        assert isinstance(out, str)

    def test_cross_file_lists_other_candidates(self, tmp_path, monkeypatch):
        d = tmp_path / "logs"
        d.mkdir()
        (d / "a.log").write_text("timeout error\n", encoding="utf-8")
        (d / "b.log").write_text("timeout\n", encoding="utf-8")
        monkeypatch.setattr(tools, "LOG_DIR", d)
        out = tools.read_log(keywords=["timeout"])
        assert out.startswith("[来源: a.log")
        assert "[其他命中文件: b.log (1/1)]" in out

    def test_no_other_files_when_service_pinned(self, log_dir):
        out = tools.read_log(service="order-service", keywords=["timeout"])
        assert "其他命中文件" not in out


class TestSearchCode:
    def test_hit_format_file_line(self, code_dir):
        out = tools.search_code("POOL_MAX")
        assert "--- src/db.py ---" in out
        assert "POOL_MAX = 1" in out
        assert "处命中" in out

    def test_miss_returns_hint(self, code_dir):
        out = tools.search_code("zzz-no-hit")
        assert out.startswith("[代码中未命中")


class TestReadConfig:
    def test_lists_config_metadata(self, tmp_path, monkeypatch):
        (tmp_path / "config.yaml").write_text("a: 1\nb: 2\n", encoding="utf-8")
        monkeypatch.setattr(tools, "CONFIG_DIR", tmp_path)
        out = tools.read_config()
        assert "config.yaml" in out and "大小" in out and "2 行" in out and "最后修改" in out

    def test_rejects_path_and_handles_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tools, "CONFIG_DIR", tmp_path)
        assert tools.read_config("../secret") .startswith("[错误]")
        assert tools.read_config("missing.yaml").startswith("[未命中]")


class TestGitTools:
    """对真实靶场仓库的冒烟测试（integration）。"""

    def test_git_log_lists_commits(self):
        out = tools.git_log(10)
        assert "optimize pool config" in out

    def test_git_show_rejects_injection(self):
        for bad in ["", "abc; rm -rf /", "12345g", "abc", "x" * 41]:
            out = tools.git_show(bad)
            assert out.startswith("[错误]"), f"未拦截: {bad!r}"

    def test_git_show_valid_commit(self):
        out = tools.git_show("783f100")
        assert out.startswith("[git show 783f100]")
        assert "POOL_MAX" in out


class TestDraftTicket:
    """write 级工具：写文件副作用 + 错误不抛异常。"""

    def test_writes_markdown_file(self, tmp_path, monkeypatch):
        d = tmp_path / "tickets"
        monkeypatch.setattr(tools, "TICKET_DIR", d)
        out = tools.draft_ticket(
            title="订单服务 503：连接池耗尽",
            severity="high",
            root_cause="src/db.py:14 POOL_MAX=1 导致并发打满",
            suggested_fix="将 POOL_MAX 提升至 20 并加连接超时",
        )
        assert out.startswith("[工单已创建]")
        files = list(d.glob("*.md"))
        assert len(files) == 1
        body = files[0].read_text(encoding="utf-8")
        assert "严重程度: high" in body
        assert "POOL_MAX=1" in body
        assert "连接超时" in body

    def test_write_failure_returns_error_prefix(self, tmp_path, monkeypatch):
        # 目录被文件占位 → mkdir 失败 → [错误] 前缀，不抛异常
        blocker = tmp_path / "tickets"
        blocker.write_text("not a dir", encoding="utf-8")
        monkeypatch.setattr(tools, "TICKET_DIR", blocker / "sub")
        out = tools.draft_ticket("t", "low", "rc", "fix")
        assert out.startswith("[错误]")


class TestRiskLevels:
    """风险分级契约：draft_ticket 是 write 级（触发审批），其余只读工具是 read 级。"""

    def test_draft_ticket_is_write_level(self):
        assert tool_level("draft_ticket") == "write"

    def test_known_tools_are_read_level(self):
        for name in ("read_log", "search_code", "read_config", "git_log", "git_show", "load_skill"):
            assert tool_level(name) == "read", name

    def test_unknown_tool_fails_closed_to_write(self):
        # fail-closed：没注册过的工具当有副作用处理，走审批
        assert tool_level("no_such_tool") == "write"

    def test_all_registered_tools_have_level(self):
        for name, spec in TOOL_REGISTRY.items():
            assert spec.get("level") in ("read", "write", "danger"), name
