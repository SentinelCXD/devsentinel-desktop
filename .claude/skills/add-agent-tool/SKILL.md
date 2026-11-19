---
name: add-agent-tool
description: 给 DevSentinel Agent 新增一个工具时使用。覆盖完整标准流程：职责与权限定级、tools.py 纯函数契约、registry.py 的 Args 模型与注册、契约测试、MCP 暴露决策与验证。适用于用户要求"加一个工具"（如新增 list_services）或修改现有工具行为的任务。
---

# 新增一个 Agent 工具的标准流程

按以下 6 步执行，每步的契约不可省略。参考实现：`load_skill`
（`backend/services/agent/skills.py` + `registry.py` 中的注册 + `tests/test_skills.py`），
它是最新最完整的样例。

## 第 1 步：定职责与权限级别

- 用一句话写清工具做什么、解决排查的哪个环节（找不到证据时模型靠它换路径）
- **把任务 prompt 里的每个需求点列成清单**（如"服务名、文件名、大小、行数"），
  实现时逐条对照——合规但漏需求比不合规更隐蔽
- 定 `level`：
  - `read`：纯查询，无副作用（日志/代码/git/手册类）
  - `write`：有副作用（写文件、发请求）→ 执行前会挂起等人工审批（HITL）
  - `danger`：不可逆/影响外部系统
  - 拿不准就往严了定；审批不是缺陷，是功能

## 第 2 步：tools.py 写纯函数

文件：`backend/services/agent/tools.py`（与新概念强相关的可独立成模块，如 `skills.py`）

必须遵守的契约：
- **返回 `str`、永不抛异常**——错误信息也是喂回模型的 observation，
  模型靠它自我修正；抛异常会中断整个 Agent 循环
- 错误用约定前缀：`[错误]`（参数错/执行失败）、`[未命中]`/`[无日志]`（换检索策略）
- 成功路径给出**证据格式**的输出：来源（文件名）、命中情况、`文件:行号`
- 输出过 `_truncate()` 截断（复用 tools.py 的，行数 + 4000 字符双上限）
- 有外部路径的（目录/仓库）用模块级常量（如 `LOG_DIR`），测试要 monkeypatch
- subprocess / 文件 IO 的失败要捕获后返回 `[错误]` 前缀字符串

## 第 3 步：registry.py 写 Args 模型并注册

文件：`backend/services/agent/registry.py`

- Pydantic `BaseModel`，类名 `<ToolName>Args`
- 每个 `Field(description=...)` 是**写给模型看的说明书**：写清语义、限制、
  防呆规则（如"必须从 git_log 结果中取 hash，不支持 HEAD~1"）、
  未命中/失败时的降级策略
- 枚举值用 `Literal`
- 注册进 `TOOL_REGISTRY`：`level` / `func` / `args_model` / `description`
  （description 写使用时机和与其他工具的衔接，别复述参数）

## 第 4 步：契约测试

文件：`backend/tests/test_tools.py`（或独立 `test_<模块>.py`）

必测场景：
1. 正常路径：输出含证据要素（来源/行号/命中数）
2. 错误路径返回 `[错误]` 前缀且**不抛异常**（用非法参数直接调函数）
3. 边界：空输入、超长输出截断标记、不存在的外部资源
4. 权限：加进 `TestRiskLevels.test_known_tools_are_read_level`
   （write 级则参照 draft_ticket 的写法断言）

## 第 5 步：MCP 暴露决策

文件：`backend/mcp_server.py`。默认**不暴露**，只有满足两条才加：
对外部 MCP Host 有独立价值，且与已暴露工具不重叠（grep_logs 就因与
read_log 重叠而没暴露）。暴露时复用 `TOOL_REGISTRY[...]["description"]`，
只读工具加 `READ_ONLY` 注解。

## 第 6 步：验证

```bash
cd backend && .venv/bin/python -m pytest -q        # 必须全绿
```

可选（改了核心链路时）：起后端后跑
`.venv/bin/python eval/run_baseline.py` 对比 `eval/baseline-v1.md` 命中率不回退。

## 完成清单（交付前自查）

- [ ] **逐条核对任务 prompt 的每个需求点都覆盖了**（不只是"写了个合规工具"）
- [ ] 函数契约：str 返回 / 不抛异常 / 错误前缀 / _truncate
- [ ] Args 的 description 写了限制与降级策略
- [ ] TOOL_REGISTRY 注册且 level 正确
- [ ] 测试覆盖正常/错误/边界 + 权限级别
- [ ] pytest 全绿
- [ ] MCP 暴露与否是主动决策（不是忘了）
- [ ] 新工具必须让模型能发现它：system prompt 工具清单 / TOOL_REGISTRY
      description / 其他索引任一通道可达（模型不知道的工具等于不存在）
