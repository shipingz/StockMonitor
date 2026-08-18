# ADR-0017: 外部调度器触发 — cron-job.org 每 5 分钟调用 workflow_dispatch（schedule 降级为兜底）

- 状态：**已确认**
- 日期：2026-08-17（讨论确认）

## 背景

实时价模式（ADR-0016）要求交易时段每 5 分钟检测一次，但 GitHub Actions 原生 `schedule` cron 实测无法支撑该频率：

- 2026-08-17（周一，交易日）全天 schedule 触发仅 7 条：10:19、11:24、12:11、13:36、14:04、15:08、16:04——间隔完全不成节奏（最差 56 分钟），5 分钟粒度应有的 ~45 条实际只到 2 条（15:08/16:04）。
- 官方文档承认 schedule 事件在高负载时段（整点）会延迟；社区大量实测 5 分钟粒度触发率极低（[讨论 #170165](https://github.com/orgs/community/discussions/170165)、[Actions 限制文档](https://docs.github.com/en/actions/reference/limits)）。
- GitHub 状态页显示 Actions 事故频繁（7-09/7-19/7-25/8-06/8-17 五次 critical，[githubstatus.com](https://www.githubstatus.com)）；8-17 当晚还有一场含 Actions 的 critical 事故进行中——但白天触发稀少发生在事故之前，属「常态不可靠」而非单日故障。

结论：5 分钟粒度不能押在 GitHub schedule 上。GitHub 提供更可靠的触发方式 `workflow_dispatch` REST API（事件驱动、无 schedule 节流层）。

## 事实约束（已查证）

- dispatch API：`POST /repos/{owner}/{repo}/actions/workflows/{workflow_file}/dispatches`，需 token 且权限 `actions:write`，成功返回 204（[REST 文档](https://docs.github.com/zh/rest/using-the-rest-api/rate-limits-for-the-rest-api)）。
- 速率限制：认证后核心 API 5000 次/小时；本项目 12 次/小时（0.24%），无压力。
- 触发行为：dispatch 创建后 job **必会运行**（排队等 runner 而非被跳过）；从调用到 job 启动通常 5~30 秒，高峰可能排队数分钟。
- 并发：免费账号并发 job 上限 20；本项目每 5 分钟 1 个 job、单 job 1~2 分钟，远不触顶；workflow 已有 `concurrency` 组防重叠。
- cron-job.org 免费版支持分钟级计划，HTTP 自定义方法/头/体（[实践参考](https://lindsay.codes/posts/cron-job-dot-org/)）。

## 决策

1. **外部调度**：cron-job.org 注册 cron job——每 5 分钟 `POST` dispatch API，带 `Authorization: Bearer <PAT>`，body `{"ref":"main"}`。触发不带 inputs → workflow_dispatch 默认值（dry_run/force_run/replay 全 false）= 纯实时监控模式，与 schedule 触发等价。
2. **PAT 最小权限**：Fine-grained token，仅 StockMonitor 仓库，Permissions → Actions = Read and write，有效期 90 天。
   - 泄露边界（用户已确认可接受）：仅能触发该仓库 workflow 运行、查看运行日志（自选股/价格本就不是机密，公开仓库 variables 人人可见）；**无法**改代码/workflow、读 Secrets（GitHub 无此 API）、触及其他仓库。
   - 应急：怀疑泄露 → Settings 里删除 token 秒级失效，重新签发更新 cron-job.org 配置。
3. **schedule 保留作兜底**：workflow 中原 `*/5` schedule 不删——cron-job.org 故障时 GitHub schedule 虽不准但至少会跑几次；双源偶尔撞车由 `concurrency` 组排队消化，多跑一轮最多多一条心跳（无状态设计，ADR-0005）。
4. **脚本零改动**：所有护栏（交易日历/交易时段/非交易日退出）天然兼容任意触发时机。

## 后果

- 交易时段每 5 分钟准点检测成为现实（触发可靠性从「会缺席」变为「会迟到但不缺席」）。
- 新增外部依赖 cron-job.org + 90 天 PAT 轮换运维负担（续期是常规操作）。
- 触发延迟固定 5~30 秒 + 高峰可能排队数分钟；5 分钟粒度下无感。
- 若未来发现 cron-job.org 不可靠，可迁移到其他同类服务（配置不变，仅换 URL/凭证）。
