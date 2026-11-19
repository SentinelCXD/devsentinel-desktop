# 基线 v2（2026-11-19）：Skill 机制 + MCP 工具暴露之后

## 改动内容（相对 v1，11-16）

| 改动 | 文件 | 性质 |
|---|---|---|
| Skill 机制：`load_skill` 工具 + system prompt 手册索引（渐进式披露） | `services/agent/skills.py` / `registry.py` / `loop.py` | 🔴 新能力 |
| MySQL / K8s 两本排查手册 | `backend/skills/` | 内容资产 |
| MCP 工具暴露：`mcp__devsentinel__*`（read_log/search_code/git_log/git_show 副本）+ `mcp__filesystem__*` | `mcp_server.py` / `services/mcp/config.py` | 🔴 新通道 |
| eval 基建修复：自动放行 HITL 审批 + `approved` 计数（此前 write 工具挂起会拖死整轮） | `eval/run_baseline.py` | — |

## 运行条件

同模型、同靶场、symptom 原文单轮输入。**双臂 A/B**：A 组有 skills 目录，B 组 `mv skills /tmp`（索引为空；`load_skill` 工具仍注册在 schema 中——B 组测的是"手册内容有无"，不是"机制有无"）。每组连跑 3 轮。

## 结果

| 场景 | A 有手册（3 轮 + 1 残轮） | B 无手册（3 轮） | v1 参照（3 轮） |
|---|---|---|---|
| 01 pool-exhausted | ✅✅✅✅ | ✅✅✅ | ✅✅✅ |
| 02 zero-price-orders | ✅✅✅✅ | ✅✅✅ | ✅✅✅ |
| 03 request-hang | ❌❌❌❌ | ❌❌❌ | **✅✅✅** |
| 04 silent-logs | ✅✅❌✅ | ❌✅❌ | ✅✅✅ |
| 05 false-out-of-stock | ✅✅✅（残轮超时前诊断正确） | ✅✅✅ | ✅✅✅ |
| 自动命中率 | **11/15** | **10/15** | 15/15 |

## 分析

### 1. 03-request-hang 全线回归（0/7 vs v1 的 3/3）——本轮最重要发现

- 双臂同时失败 → 与手册无关，嫌疑指向**环境变化**
- v1 轨迹：模型只在 4 个本地工具中选路；今日轨迹：`mcp__devsentinel__` 副本被大量使用，
  03 在 B 组三轮全部走 MCP 路径且未逐个 `git_show` 可疑提交（v1 翻身的关键动作）
- **假说**：工具命名空间翻倍后模型路由质量下降——同一能力两套 schema 入口，
  模型不再稳定执行"git_log → 逐个 git_show"的深挖路径
- 待验证：隐藏 MCP 工具后重跑 03 单场景（或模型版本漂移需排除——同一 .env 配置）

### 2. load_skill 机制按设计工作，但内容领域与场景错位

- A 组 4 轮共触发 **8 次 load_skill，全部加载 mysql-troubleshooting**
  （01/03/05 的 timeout、连接类症状命中手册"适用症状"描述）
- k8s-troubleshooting **零触发**——5 个场景全部是应用层代码 bug，无 K8s 症状
- 命中率 A/B 无统计差异（11/15 vs 10/15，n=15 下 1 个净胜差属噪音）
- **结论**：机制有效（按需触发、手册正文不进 prompt），内容对当前场景集无可测增益——
  下一步若要证明手册价值，需写与场景对齐的内容（如"日志静默/应用 bug 排查路径"类手册）

### 3. eval 的两条工程教训

- **单次运行是采样不是断言**：同组命中率在 3/5–4/5 间波动（04 的成败取决于首轮
  是否误入 `mcp__filesystem__` 翻目录 60s+），3 轮×5 场景只能看方向，不能看单点
- **无人值守 eval 必须处理 HITL**：05 此前因 `draft_ticket`（write 级）挂起审批
  拖死整轮；现 run_baseline 自动放行并计 `approved` 数。副作用发现：agent 开始
  "超额完成任务"——4 个场景诊断后主动起草工单（v1 时代从未出现）

## 待办 / 下一步

- [ ] 验证 03 回归归因：临时隐藏 MCP 工具跑 03 单场景 A/B
- [ ] 写场景对齐的第三本手册（应用层 bug 排查路径），重测手册增益
- [ ] draft_ticket 的 eval 语义待定：诊断正确后起草工单应计为加分还是干扰项
