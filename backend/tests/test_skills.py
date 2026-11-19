"""skills.py 契约测试。

核心约定（Skill 机制的验收点）：
- skill_index 只含名称和描述——手册正文绝不进 system prompt；
- load_skill 永不抛异常，错误用 [错误] 前缀喂回模型（与 tools.py 同一契约）。
"""
import pytest

from services.agent import skills
from services.agent.registry import TOOL_REGISTRY, call_tool, tool_level


def write_skill(d, filename, name, description, body="手册正文\n第一步：检查连接池配置"):
    (d / filename).write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{body}",
        encoding="utf-8",
    )


@pytest.fixture
def skills_dir(tmp_path, monkeypatch):
    d = tmp_path / "skills"
    d.mkdir()
    write_skill(d, "mysql-troubleshooting.md", "mysql-troubleshooting",
                "MySQL 连接池/慢查询排查")
    write_skill(d, "k8s-troubleshooting.md", "k8s-troubleshooting",
                "K8s Pod 异常排查")
    monkeypatch.setattr(skills, "SKILLS_DIR", d)
    return d


class TestListSkills:
    def test_lists_all_sorted_by_name(self, skills_dir):
        names = [s.name for s in skills.list_skills()]
        assert names == ["k8s-troubleshooting", "mysql-troubleshooting"]

    def test_skips_invalid_frontmatter(self, skills_dir):
        (skills_dir / "broken.md").write_text("没有 frontmatter 的普通文档", encoding="utf-8")
        (skills_dir / "half.md").write_text("---\nname: only-name\n", encoding="utf-8")
        (skills_dir / "no-close.md").write_text(
            "---\nname: x\ndescription: y\n正文但没有闭合分隔线", encoding="utf-8")
        names = {s.name for s in skills.list_skills()}
        assert names == {"k8s-troubleshooting", "mysql-troubleshooting"}

    def test_missing_dir_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "nope")
        assert skills.list_skills() == []
        assert skills.skill_index() == ""


class TestSkillIndex:
    def test_index_has_name_and_description_only(self, skills_dir):
        """🔴 验收点：索引含名称+描述，但正文一个字都不能出现。"""
        idx = skills.skill_index()
        assert "mysql-troubleshooting" in idx
        assert "MySQL 连接池/慢查询排查" in idx
        assert "第一步" not in idx
        assert "连接池配置" not in idx

    def test_index_mentions_load_skill(self, skills_dir):
        assert "load_skill" in skills.skill_index()


class TestLoadSkill:
    def test_returns_full_body_without_frontmatter(self, skills_dir):
        out = skills.load_skill("mysql-troubleshooting")
        assert "第一步" in out
        assert "name:" not in out and "description:" not in out

    def test_unknown_name_returns_error_prefix_with_available(self, skills_dir):
        out = skills.load_skill("no-such-skill")
        assert out.startswith("[错误]")
        assert "mysql-troubleshooting" in out   # 可用清单喂回模型

    def test_contract_never_raises_on_missing_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "missing")
        assert isinstance(skills.load_skill("anything"), str)


class TestRegistryIntegration:
    def test_load_skill_registered_read_level(self):
        assert "load_skill" in TOOL_REGISTRY
        assert tool_level("load_skill") == "read"

    def test_call_tool_dispatches_load_skill(self, skills_dir):
        out = call_tool("load_skill", {"name": "k8s-troubleshooting"})
        assert "第一步" in out

    def test_call_tool_validates_args(self, skills_dir):
        out = call_tool("load_skill", {"wrong_field": "x"})
        assert out.startswith("[错误] 参数校验失败")


class TestRealSkills:
    """对仓库内置手册（backend/skills/）的冒烟测试（integration）。"""

    def test_all_real_skills_parse_and_unique(self):
        metas = skills.list_skills()
        assert metas, "backend/skills/ 应至少有一本手册"
        names = [s.name for s in metas]
        assert len(names) == len(set(names))
        # 约定：文件名与 name 一致，翻目录就能对上索引
        for s in metas:
            assert s.path.name == f"{s.name}.md"

    def test_real_skills_load_with_content(self):
        for s in skills.list_skills():
            out = skills.load_skill(s.name)
            assert not out.startswith("[错误]"), s.name
            assert len(out) > 100, s.name
