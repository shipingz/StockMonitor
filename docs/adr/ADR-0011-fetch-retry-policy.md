# ADR-0011: 数据抓取容错策略 — 照抄 daily_stock_analysis（串行 + 限速 + 重试）

- 状态：**已确认**
- 日期：2026-08-15（讨论确认）

## 背景

每轮 50 次 `stock_zh_a_hist_min_em` 请求（+ 1 次名称表 + 1 次交易日历），数据源为东方财富免费接口，存在限流（HTTP 429）与偶发失败风险。用户指示：重试策略直接照抄参考项目 [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis)（`data_provider/akshare_fetcher.py`）。

## 参考项目的做法（已核对其源码）

- **串行**：默认 `MAX_WORKERS=1`，不并发。
- **限速**：每次请求前随机 sleep 2~5 秒（`_enforce_rate_limit`，`sleep_min=2.0, sleep_max=5.0`）。
- **重试**：历史数据抓取（`_fetch_raw_data`）用 tenacity `stop_after_attempt(3)` + `wait_exponential(multiplier=1, min=2, max=30)`——**共 3 次尝试**，失败后按 2、4、8…秒指数退避（最大 30 秒）。
- 失败处理：单只股票失败记日志，不中断整体流程。

## 决策

全部照抄：

- 串行抓取；每次请求前随机 sleep 2~5 秒。
- 每只股票最多 **3 次尝试**（初始 + 2 次重试），指数退避 `min(2**attempt, 30)` 秒。
- 单只股票 3 次失败 → 跳过并记日志（静默，见 ADR-0008 单股失败策略）；整轮失败 → 告警（ADR-0008）。
- 单轮耗时估算：50 只 × (3.5s 限速 + ~0.5s 请求) ≈ 3 分钟，远小于 15 分钟K线间隔，串行稳定性收益 > 并发速度收益。
- 若未来自选股接近 50 只且单轮超 5 分钟，再评估并发（`MAX_WORKERS` 调参），本期不引入。

## 后果

- 单轮 50 请求 + 3 分钟耗时，稳定优先。
- 429/失败风险由限速 + 退避重试共同消化。
- 与 ADR-0008 衔接：重试耗尽后的失败按严重度分级处理。
- 注：`main.py` 中通过环境变量 `FETCH_ATTEMPTS`（默认 3）、`FETCH_SLEEP_MIN/MAX`（默认 2/5）可调。
