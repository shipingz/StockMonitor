# 部署指南（GitHub Actions 首次部署）

> 面向第一次使用 GitHub Actions 的完整步骤。共 7 步，全程约 15 分钟。
> 前提：代码在本地 `D:\StockMonitor`（git 仓库，分支 `main`），你已有一个 GitHub 账号和企业微信群机器人 webhook。

---

## 第 1 步：在 GitHub 上创建仓库

1. 浏览器打开 https://github.com/new
2. 填写：
   - **Repository name**：`StockMonitor`（随意，但建议和本地一致）
   - **Visibility**：选 **Public** ⚠️ 重要！
     - 本项目设计前提是公有仓库：Actions 分钟数**免费且不限额**（ADR-0003）
     - 私有仓库免费额度仅 2000 分钟/月，16 次/天 × ~5 分钟 × 22 交易日 ≈ 1760 分钟/月，太紧张
   - **不要勾选** "Add a README file" / ".gitignore" / "license"（本地已有，勾了会冲突）
3. 点 **Create repository**

## 第 2 步：把本地仓库推上去

打开 PowerShell，在 `D:\StockMonitor` 下执行：

```powershell
# 1. 关联远程仓库（把 <你的用户名> 和 <仓库名> 换成实际的）
git remote add origin https://github.com/<你的用户名>/StockMonitor.git

# 2. 推送（首次会弹出 GitHub 登录窗口，用浏览器登录授权即可）
git push -u origin main
```

**如果 push 报 TLS/schannel 错误**（你本机之前 clone 外部仓库时遇到过，很可能会再遇到）：
```powershell
git -c http.sslBackend=openssl -c http.proxy=http://127.0.0.1:7897 push -u origin main
```
成功后可把这两个配置固化，以后就不用每次带参数：
```powershell
git config --global http.sslBackend openssl
git config --global http.proxy http://127.0.0.1:7897
```

推送成功后刷新仓库页面，应能看到 `main.py`、`.github/workflows/monitor.yml` 等文件。

## 第 3 步：配置 Secrets（企业微信 webhook）

1. 仓库页面 → **Settings** → 左侧 **Secrets and variables** → **Actions**
2. 切到 **Secrets** 标签页 → **New repository secret**
3. 填写：
   - **Name**：`WECHAT_WEBHOOK_URL`
   - **Secret**：你的企业微信群机器人 webhook 完整 URL（形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx`）
4. 点 **Add secret**
   - ⚠️ Secret 保存后**无法查看**，只能覆盖重填——别填错

## 第 4 步：配置 Variables（自选股列表）

1. 同一个页面，切到 **Variables** 标签页 → **New repository variable**
2. 填写：
   - **Name**：`STOCK_LIST`
   - **Value**：逗号分隔的纯 6 位代码，如 `600519,000001,300750`（≤50 只）
3. 点 **Add variable**
   - Variable 是明文，随时可以查看/修改（以后改自选股就改这里，不用动代码）

## 第 5 步：手动触发 dry-run 验证（不推送，先看结果）

1. 仓库页面 → **Actions** 标签
2. 左侧点 **A股15分钟K线MA60监控**（workflow 名称）
3. 右侧点 **Run workflow** 按钮 → 展开参数：
   - **Dry-run**：勾选 ✅
   - 其他不勾
4. 点 **Run workflow**，然后点击刚出现的运行记录，进入日志页
5. 等待 1~2 分钟，检查日志：
   - 看到 `安装依赖` 步骤成功（pip 装 akshare）
   - 看到 `执行监控` 步骤输出 `dry_run=True ... 检查 N 只` 和 `[dry-run] 将推送（N 字节）` 的消息全文
   - 一切正常 → 第 6 步

## 第 6 步：真实推送验证（非交易日也能验）

非交易日（周末/节假日）时，最直观的端到端验证是**复盘模式**：

1. 再次 **Run workflow**：
   - **Replay**：勾选 ✅（复盘最近完整交易日全部信号）
   - **Replay date**：留空（自动）
   - Dry-run：**不要勾选**
2. 运行完成后检查企业微信群：应收到一条 `## 📊 信号复盘：xxxx-xx-xx（周X）` 消息
   - 收到 → 推送链路 ✅、抓取链路 ✅、判定链路 ✅，全部打通
   - 没收到 → 看第 8 步排查

## 第 7 步：等交易日自动运行

- 从下一个交易日起，workflow 会在每根 15 分钟K线收盘后 3 分钟自动触发（北京时间 9:48 ~ 15:03，16 轮/天）
- 每轮必推一条：有信号推信号，无信号推 `✅ 系统正常` 心跳
- 每天 15:03 后收到心跳，即代表当日 16 轮全部正常
- 某天一条消息都没有：要么那天非交易日，要么系统故障（查 Actions 运行记录）

---

## 第 8 步：常见问题排查

| 症状 | 原因与处理 |
|---|---|
| push 报 `schannel: AcquireCredentialsHandle failed` | 本机 TLS/代理问题，用第 2 步的 `-c http.sslBackend=openssl -c http.proxy=...` 命令，或固化全局配置 |
| Actions 页看不到 workflow | 确认 push 到了默认分支 `main`；仓库刚创建时 Actions 可能要等几十秒才出现 |
| 运行日志里「抓取失败 / 全部失败」 | 看具体错误：HTTP 429 是限流（重试会自愈）；连接超时多为 GitHub runner 访问东财偶发抖动，下轮自动恢复；持续失败可检查是不是数据源接口变更 |
| 收到「⚠️ 监控异常：交易日历获取失败」 | 新浪日历接口偶发失败，ADR-0008 设计行为：按交易日继续跑，下轮自愈；频繁出现再排查 |
| 运行日志里 `Non-trading day` / 「非交易日，退出」 | 正常行为（ADR-0006），非交易日 0 推送 |
| 想改自选股 | Settings → Variables → 编辑 `STOCK_LIST`，无需 push 代码，下一轮自动生效 |
| 想临时验证某天信号 | 手动 Run workflow → Replay + Replay date 填 `YYYY-MM-DD` |
| 仓库 60 天无活动被禁用定时任务 | GitHub 自动暂停 schedule；重新进入 Actions 页手动 Run 一次即恢复（本项目设计不做 keepalive） |

---

## 验证清单（全部 ✅ 即部署完成）

- [ ] 仓库 Public，代码已 push，`monitor.yml` 存在
- [ ] Secrets 有 `WECHAT_WEBHOOK_URL`
- [ ] Variables 有 `STOCK_LIST`
- [ ] 手动 dry-run 一次：日志正常、消息内容正确
- [ ] 手动 replay 一次（非 dry-run）：企业微信收到复盘消息
- [ ] 下一个交易日自动运行，收到 `✅ 系统正常` 心跳
