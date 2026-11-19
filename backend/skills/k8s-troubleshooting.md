---
name: k8s-troubleshooting
description: K8s 排查手册：CrashLoopBackOff、OOMKilled、Pending、探针失败的定位命令与常见根因
---
# K8s 排查手册

适用症状：Pod 反复重启、起不来、一直 Pending、服务不健康被摘除流量。

总原则：先 `kubectl describe pod` 看 Events（最后一行通常就是答案），
再 `kubectl logs` 看容器输出。Restart Count > 0 时日志要加 `--previous`。

## 1. CrashLoopBackOff（容器反复崩溃）

定位：
1. `kubectl describe pod <pod>`：看 Last State 的 Exit Code
2. `kubectl logs <pod> --previous`：上一次崩溃前的输出（关键）
3. 常见 Exit Code：1=程序异常；137=OOMKilled（转第 2 节）；
   126/127=命令或权限问题

常见根因：配置错误（连不上 DB/配置格式错）、启动时依赖未就绪、
代码抛未捕获异常退出。

## 2. OOMKilled（内存不足被杀）

定位：`kubectl describe pod` 里 Last State: Terminated, Reason: OOMKilled，
Exit Code 137。

常见根因：容器 memory limit 设置过低；内存泄漏（RSS 持续上涨直到超限）；
流量高峰超预估。注意与"应用自己的超时杀进程"区分——Reason 是否 OOMKilled。

修复：先确认是否泄漏（监控看趋势），单纯调大 limit 前先想清楚上限依据。

## 3. Pending（调度不上去）

定位：`kubectl describe pod` Events 段：
- `Insufficient cpu/memory`：节点资源不够
- `node(s) didn't match ...`：节点选择器/亲和性不匹配
- `volume node affinity conflict` / PVC 未绑定：存储问题
- `ImagePullBackOff`：镜像拉不下来（tag 错 / 认证过期 / 仓库限流）

## 4. 探针失败（Unhealthy / 就绪被摘）

区分两种探针的后果：
- readiness 失败：Pod 不接流量但不重启（服务"消失"）
- liveness 失败：kubelet 重启 Pod（表象同 CrashLoop，但程序其实没崩）

常见根因：探针超时设太短（冷启动没预热完就被判死）、探针路径错了、
依赖不可用导致健康检查返回非 200。

## 5. 排查命令速查

- `kubectl get pods -o wide`：整体状态 + 所在节点
- `kubectl describe pod <pod>`：Events + 探针配置 + 重启计数
- `kubectl logs <pod> [--previous] [-f]`：应用输出
- `kubectl top pod`：当前资源用量（对比 limit）
- `kubectl get events --sort-by=.lastTimestamp`：按时间看全局事件

## 6. 用 DevSentinel 工具怎么查

容器化服务的故障仍从日志与变更入手：
1. read_log：搜 restart、oom、unhealthy、probe、exit
2. git_log：找 deployment / limits / 探针配置类提交（如 "reduce memory"）
3. search_code：搜探针路径、health check、资源配置字段名
