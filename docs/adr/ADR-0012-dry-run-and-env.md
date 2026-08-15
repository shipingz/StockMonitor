# ADR-0012: 手动触发与测试 — workflow_dispatch + dry-run 开关

- 状态：**已确认**
- 日期：2026-08-15（讨论确认）

## 背景

用户需要手动触发机制用于测试（改自选股后验证、调休周末手动补跑），需要决定是否提供不推送的 dry-run 模式，以及 Python 开发环境形态。

## 决策

1. **`workflow_dispatch` 手动触发**（workflow 级，非仅 schedule）：
   - 输入项 `dry_run`（boolean，默认 false）：为 true 时脚本只抓数据、计算、打印结果，**不推送任何企业微信消息**（信号、心跳、告警均不推）。
   - 输入项 `force_run`（boolean，默认 false）：跳过交易日历检查强制运行（沿用参考项目设计，供调试）。
2. **dry-run 行为**：输出「本应推送的消息全文」到日志（含信号/心跳/告警三态），供人工核对；退出码正常。
3. **Python 环境**（照抄参考项目）：
   - Python 3.11（`actions/setup-python@v6` + `cache: 'pip'`）。
   - 依赖清单 `requirements.txt`：`akshare>=1.12.0`、`pandas`、`requests`（及传递依赖），版本下限约束 + 定期升级，不锁死补丁版（参考项目风格）。
   - `actions/checkout@v5` 检出代码。
4. 与 ADR-0003 衔接：schedule 与 workflow_dispatch 共用同一 job/脚本入口；`dry_run` 仅手动触发时存在。

## 后果

- 用户可安全验证配置修改（改 `STOCK_LIST` 后手动 dry-run 一轮，确认股票名称匹配、MA60 计算无误，再切回真实运行）。
- 调休周末可手动 `force_run` 补监控（ADR-0006 的兜底路径）。
- 依赖不锁补丁版：升 akshare 版本可能带来接口行为变化，需关注 Release 说明（已知分钟数据近端限制等，见 ADR-0004）。
