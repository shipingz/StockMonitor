# ADR-0015: 东方财富反爬补丁 + 新浪降级源

- 状态：**已确认（2026-08-15 修订：新增新浪降级源，解决 anonflow2 403）**
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
   - anonflow2 换令牌请求带浏览器特征头（Referer/Origin/Accept/Accept-Language）降低 403 概率。
2. **名称表主源改新浪批量**（2026-08-15 修订）：`fetch_name_map(codes)` 优先用 `hq.sinajs.cn/list=sh600519,...` **一次请求查全部自选股名称**（GBK 解码；参考项目同款），避免东财全市场分页（akshare 内部 tqdm 17 页，慢且触发风控）；新浪失败才 fallback 东财 `stock_info_a_code_name`（重试 `FETCH_ATTEMPTS` 次，指数退避）。
3. **K 线主源东财 + 新浪降级**（2026-08-15 修订）：`fetch_15min_kline` 东财（带补丁+重试）失败后自动切**新浪直连** `quotes.sina.cn/cn/api/jsonp_v2.php/.../CN_MarketDataService.getKLineData?symbol=sh600519&scale=15&datalen=200`（返回近 200 根 15 分钟K线 ≈ 12.5 交易日，足够 61 根门槛），解析 JSONP 为 时间/收盘 两列；新浪源同样带重试。
4. 限速叠加说明：补丁随机休眠 1~4s + 原有 `enforce_rate_limit` 2~5s → 每请求约 3~9s，50 只单轮约 3~7.5 分钟，仍在 15 分钟K线窗口内（ADR-0011）。

## 后果

- GitHub Actions 上东财接口恢复可用（与参考项目在 Actions 上的成功实践一致）；即使东财/令牌接口被彻底封死，新浪降级源保证核心功能可用（本机已实测：名称接口与 200 根分钟K线均正常返回）。
- 新浪源数据特征：`getKLineData` 返回近 200 根（约 12.5 个交易日），比东财近 5 日窗口更长；字段仅 day/OHLC/volume，本项目只用 时间/收盘，无影响。
- 东财风控方案可能变化（nid 接口本身也可能失效）；若补丁失效，日志会出现「nid 令牌获取失败」警告，可设 `EASTMONEY_PATCH=0` 关闭补丁排查，或等待 akshare/社区更新方案。
- 补丁为全局 monkeypatch，但严格限定东财域名，其他请求零影响。
