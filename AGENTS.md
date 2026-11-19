# DevSentinel

AI 值班助手：**手写 Agent 循环**（不依赖框架）做故障诊断——读日志、搜代码、查 git 变更，
定位根因后起草工单（write 级操作走人工审批 HITL）。后端 FastAPI，前端 React + Electron，
自带故障靶场（5 个注入场景）与评测基线。

## 目录结构

```
backend/
  server.py              FastAPI 入口 + SSE 流式接口
  services/agent/
    loop.py              Agent 循环：事件流、HITL 审批挂起、token 预算
    tools.py             工具函数（纯程序实现，不调模型）
    registry.py          工具注册表：Args 模型 + 注册 + call_tool 分发
    skills.py            Skill 机制：手册渐进式加载（索引进 prompt，全文按需注入）
    workflow.py          固定三步流水线（baseline，非 Agent）
  services/llm/          LLM 客户端（OpenAI 兼容接口，DeepSeek）
  services/mcp/          MCP client（连接外部 MCP server）
  mcp_server.py         把诊断工具暴露成 MCP server（stdio）
  skills/                排查手册（内容资产：AI 起草、人工审阅后生效）
  fault_lab/            故障靶场：场景 yaml + 假日志 + 嵌套 git 仓库（独立管理，勿提交）
  eval/                  评测跑批脚本 + 基线报告
  tests/                 pytest
frontend/                React 19 + Vite + Electron（诊断时间线 UI）
```

## 常用命令

```bash
# 后端测试（提交前必须全绿）
cd backend && .venv/bin/python -m pytest -q

# 启动后端（端口 8000；.env 里配 LLM_API_KEY）
cd backend && .venv/bin/python -m uvicorn server:app --reload

# 前端（Vite + Electron）
cd frontend && npm install && npm run dev

# 评测（需先起后端；输出 eval/runs/<时间戳>/report.md）
cd backend && .venv/bin/python eval/run_baseline.py

# MCP server 调试
cd backend && .venv/bin/python mcp_server.py   # stdio，由 Host 拉起
```

## 代码规范

**Agent 工具契约**（`tools.py` / `skills.py` / 一切会被 `call_tool` 调用的函数）：
- 返回值永远是 `str`，**永不抛异常**——错误也是喂回模型的 observation
- 约定前缀：`[错误]`（可修正重试）、`[未命中]`/`[无日志]`（换策略）、`[已创建]` 等
- 给模型的输出必须过 `_truncate` 截断（行数 + 字符数双上限），防爆上下文

**新增工具三件套**（`registry.py` 模式）：
1. `tools.py` 写纯函数（副作用目录用模块级常量，便于测试 monkeypatch）
2. `registry.py` 写 Pydantic Args 模型——`Field(description=...)` 就是给模型看的
   使用说明，要写清限制、防呆规则和未命中时的降级策略
3. `TOOL_REGISTRY` 注册一行，标 `level`

**权限分级**：`read`（直接执行）/ `write`（HITL 审批后执行）/ `danger`。
未知工具按 `write` 处理（fail-closed）。

**其他**：
- Python 3.10+ 类型标注；docstring 用中文、解释"为什么"而不是复述代码
- 测试风格：契约测试 + mock 外部依赖（`test_loop.py` 的 FakeLLM/FakeTools 模式），
  对真实靶场的冒烟测试单独归类（如 `TestGitTools`）
- MCP SDK 版本 pin 在 `mcp>=1,<2`（v2 改名不兼容）
- 前端 React 19 + Tailwind，事件类型以后端 `agent_loop` 的 yield 字段为准

## 🔴🟡🟢 分工规则（AI 自主程度）

| 级别 | 定义 | AI 可做的事 | 人工介入 |
|---|---|---|---|
| 🟢 | 不改变行为、有测试兜底 | 新增测试、文档、注释、纯格式、不改逻辑的重构 | 事后抽查即可 |
| 🟡 | 改变行为、可回滚 | 新工具/新功能、业务逻辑修改、prompt 文案、依赖变更 | **必须 review 后合并**，AI 需说明改了什么/为什么/怎么验证 |
| 🔴 | 高风险或不可逆 | HITL 审批逻辑、权限分级（level 字段）、安全与密钥、删除数据、公共 API 契约、架构决策 | **人工主导，AI 只提方案不动代码** |

通用规则：
- 任何改动提交前 `cd backend && .venv/bin/python -m pytest -q` 必须全绿
- 拿不准级别的，按更严一级处理
- `backend/skills/` 下的手册是内容资产：AI 起草 → 人工审阅 → 才算生效

## Skill

新增 Agent 工具的标准流程见 [`.claude/skills/add-agent-tool/SKILL.md`](.claude/skills/add-agent-tool/SKILL.md)。
Claude Code 会自动加载该 Skill；其他工具（Codex 等）按其中流程执行即可。
