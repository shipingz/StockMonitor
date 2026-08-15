# ADR-0009: 配置管理 — Secrets 管机密，Variables 管配置

- 状态：**已确认**
- 日期：2026-08-15（讨论确认）

## 背景

用户要求用 GitHub Actions 的 Repository 级配置维护：企业微信 webhook 链接、自选股列表、以及将来可能添加的设置项（MA 周期、重试次数、并发数、消息开关等）。

## 事实约束（已查证）

- **Repository secrets**：加密存储，**不可查看**，修改需重新输入完整值；大小 ≤64KB。
- **Repository variables**：明文存储，UI 可直接查看/随时编辑；大小 ≤64KB。
- workflow 中读取：`${{ secrets.XXX }}` 与 `${{ vars.XXX }}`；代码侧均以环境变量形式注入。
- 公开仓库中，有 push 权限者可改 workflow 窃取 secrets——个人项目风险可接受，知悉即可。

## 决策

按「机密性」分流，而非全部使用 secrets：

| 配置项 | 存放位置 | 说明 |
|---|---|---|
| `WECHAT_WEBHOOK_URL` | **Secrets** | 真机密，进了 git 历史即失效 |
| `STOCK_LIST`（自选股，逗号分隔代码） | **Variables** | 非机密、需频繁修改（secrets 不可查看 + 每次重输，摩擦大） |
| 将来的设置项（MA 周期、重试次数、并发、开关等） | **Variables** | 同上；代码统一 `os.environ.get("XXX", 默认值)` 读取 |

- workflow 统一使用**三段式兼容**写法：`${{ vars.XXX || secrets.XXX || '默认值' }}`，兼容两种存放位置。
- 代码侧不做任何差异处理：一律从环境变量读取，默认值兜底（缺配置不崩溃，走默认）。
- 自选股列表格式：逗号分隔纯 6 位代码（`600519,000001,300750`），脚本解析后校验。

## 后果

- 新增设置项 = workflow 加一行 env 映射 + 代码读一行 `os.environ.get`，无需改架构。
- 用户修改自选股：GitHub UI → Settings → Secrets and variables → Actions → Variables → 编辑 `STOCK_LIST`，无需 push 代码。
- 废弃方案：仓库内 `stocks.txt` 配置文件（改配置需 push 代码，摩擦大）；全部塞 secrets（改列表体验差）。
