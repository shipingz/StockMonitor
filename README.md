# A股 15分钟K线 MA60 上穿监控

在 GitHub Actions 上定时运行的 A 股监控工具：每个交易日 16 根 15 分钟K线收盘后 3 分钟触发检查，检测自选股「收盘价上穿 MA60」信号，通过企业微信机器人和/或飞书群机器人 Webhook 推送（有哪个推哪个）。

设计决策全部记录在 [CONTEXT.md](CONTEXT.md)（术语表/决策索引/事实约束）与 [docs/adr/](docs/adr/)（ADR-0001 ~ ADR-0014）。

## 工作原理

- **调度**：cron 精确对齐 16 个K线收盘时刻后 3 分钟（北京时间 9:48 ~ 15:03），交易日 16 轮/天；法定节假日由脚本内交易日历剔除（非交易日 0 推送）。
- **信号**：`收盘价上穿 MA60`——当前K线收盘价 > MA60（最近 60 根含当前），且上一根K线收盘价 ≤ 上一时刻 MA60（[ADR-0002](docs/adr/ADR-0002-trigger-semantics.md)）。
- **消息三态**（每轮必推一条，[ADR-0010](docs/adr/ADR-0010-heartbeat-push.md)）：
  - 📈 **信号**：有上穿信号时（多只合并一条，超长自动拆分）
  - ✅ **心跳**：无信号时推送「系统正常」确认
  - ⚠️ **告警**：整轮抓取失败 / 交易日历失败 / 严重延迟
  - 推送渠道：企业微信（markdown）与/或飞书（interactive 卡片 + lark_md，text 降级），有哪个推哪个（[ADR-0014](docs/adr/ADR-0014-multi-channel-push.md)）
- **无状态**：不做跨运行持久化（[ADR-0005](docs/adr/ADR-0005-stateless-design.md)），去重由调度频率天然保证。
- **容错**：串行抓取 + 随机 2~5 秒限速 + 指数退避重试（最多 3 次尝试），照抄参考项目 [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)（[ADR-0011](docs/adr/ADR-0011-fetch-retry-policy.md)）。

## 配置（一次性）

在 GitHub 仓库 **Settings → Secrets and variables → Actions** 中配置：

| 配置项 | 位置 | 示例 | 说明 |
|---|---|---|---|
| `WECHAT_WEBHOOK_URL` | **Secrets** | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx` | 企业微信群机器人 Webhook（可选，配置了才推企微） |
| `FEISHU_WEBHOOK_URL` | **Secrets** | `https://open.feishu.cn/open-apis/bot/v2/hook/xxx` | 飞书群机器人 Webhook（可选，配置了才推飞书） |
| `FEISHU_WEBHOOK_SECRET` | **Secrets** | `xxxx` | 飞书机器人「加签」密钥（可选，仅在机器人开启加签时配置） |
| `STOCK_LIST` | **Variables** | `600519,000001,300750` | 自选股，逗号分隔纯 6 位代码，≤50 只 |

推送渠道规则（[ADR-0014](docs/adr/ADR-0014-multi-channel-push.md)）：**有哪个推哪个，都有都推**；两个都没配且非 dry-run 时报错退出。

将来新增设置项（MA 周期、重试次数、消息阈值等）统一放 **Variables**，代码从环境变量读取并带默认值（[ADR-0009](docs/adr/ADR-0009-config-management.md)）。

## 使用

- **日常**：什么都不用做，每个交易日自动运行并推送。
- **测试**：Actions → 手动运行 workflow → 勾选 `dry_run`（只抓取计算不推送，结果在运行日志中可见）；`force_run` 可跳过交易日历检查（如调休周末补跑）。
- **信号复盘**（[ADR-0013](docs/adr/ADR-0013-replay-mode.md)）：非交易日想验证最近一个完整交易日的全部信号时，手动运行 workflow → 勾选 `replay`（可选填 `replay_date` 指定日期 `YYYY-MM-DD`，留空自动取最近完整交易日——今天已收盘则取今天）。结果推一条复盘消息，日志含每只股票全天 16 根K线明细；可搭配 `dry_run` 只打印不推送。
- **改自选股**：编辑 Variables 里的 `STOCK_LIST`，无需改代码。

## 本地开发

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v   # 纯逻辑测试（不依赖网络）
```

本地模拟运行（需自行设置环境变量，`DRY_RUN=1` 时不推送）：

```bash
set WECHAT_WEBHOOK_URL=...   # Windows
set STOCK_LIST=600519,000001
set DRY_RUN=1
python main.py
```

## 已知边界（详见 ADR-0005）

- 某轮运行失败 → 该根K线漏检（GitHub Actions 不重试 schedule）。
- job 排队延迟跨过K线边界（罕见）→ 可能偶发重复 1 条或漏 1 根。
- 仓库 60 天无 activity → GitHub 自动禁用定时任务（本项目不做 keepalive，用户每日查看推送，若发现被禁用手动重新启用即可）。

## 免责声明

本项目仅为个人监控提醒工具，不构成任何投资建议。
