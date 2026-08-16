# ADR-0015: 东方财富反爬补丁 + 名称表重试

- 状态：**已确认**
- 日期：2026-08-15（部署后修复，讨论确认）

## 背景

部署到 GitHub Actions 后实测出现两个问题（2026-08-16）：

1. `stock_info_a_code_name()`（代码名称表）单次失败即放弃：`ConnectionResetError(104)`，名称退化为代码。
2. 全部自选股 `stock_zh_a_hist_min_em` 重试 3 次仍全部失败：`RemoteDisconnected('Remote end closed connection without response')`。

## 根因（已查证）

- 东方财富对**云服务器 IP**（GitHub Actions runner 为微软云）实施风控：直连时在 TLS/HTTP 层直接断连（RemoteDisconnected / Connection reset），重试多少次都一样——这是 IP 层面的拒绝，不是偶发故障。
- 参考项目 [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) 专门为此写了 `eastmoney_patch.py`：请求东财前先向 `anonflow2.eastmoney.com/backend/api/webreport` 换取 **nid 授权令牌**，抓取时携带 `Cookie: nid18=<nid>` + 随机浏览器 UA + 随机休眠 1~4 秒，即可通过风控。
- `stock_info_a_code_name()` 数据源同为东财（push2.eastmoney.com），与问题 2 同源。

## 决策

1. **移植东财补丁**（默认开启，`EASTMONEY_PATCH` 环境变量可关）：
   - monkeypatch `requests.Session.request`，仅对东财域名（`fund/push2/push2his.eastmoney.com`）生效，不影响新浪日历/飞书/企业微信等其余请求。
   - 对东财请求：附加随机 UA（**内置 6 个浏览器 UA 池，替代参考项目的 fake_useragent**，避免额外网络依赖）+ nid18 cookie（20 秒缓存）+ 随机休眠 1~4 秒。
   - nid 换取失败时降级为「无令牌直连」（尽力而为，日志警告）。
2. **名称表加重试**：`fetch_name_map` 与 K 线抓取一致——`FETCH_ATTEMPTS`（默认 3）次，指数退避 `min(2**attempt, 30)`；最终失败才返回空映射（名称退化为代码，ADR-0001）。
3. 限速叠加说明：补丁随机休眠 1~4s + 原有 `enforce_rate_limit` 2~5s → 每请求约 3~9s，50 只单轮约 3~7.5 分钟，仍在 15 分钟K线窗口内（ADR-0011）。

## 后果

- GitHub Actions 上东财接口恢复可用（与参考项目在 Actions 上的成功实践一致）。
- 东财风控方案可能变化（nid 接口本身也可能失效）；若补丁失效，日志会出现「nid 令牌获取失败」警告，可设 `EASTMONEY_PATCH=0` 关闭补丁排查，或等待 akshare/社区更新方案。
- 补丁为全局 monkeypatch，但严格限定东财域名，其他请求零影响。
