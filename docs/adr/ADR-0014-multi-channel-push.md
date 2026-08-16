# ADR-0014: 多渠道推送 — 企业微信 + 飞书，有哪个推哪个

- 状态：**已确认**
- 日期：2026-08-15（讨论确认）

## 背景

用户实际使用飞书群机器人（而非企业微信），要求：保留企业微信推送能力，新增飞书推送；未配置的渠道不推送；配置了哪个就推哪个，都配置了就都推送。

## 事实约束（已查证）

- 飞书自定义机器人 webhook：`https://open.feishu.cn/open-apis/bot/v2/hook/<key>`。
- 飞书 webhook 支持 `interactive`（卡片，`elements[].tag=div` + `text.tag=lark_md` 可渲染 markdown）、`text`（纯文本）等消息类型；**lark_md 不支持 `#` 标题语法**，需转换（参考项目 `format_feishu_markdown`：标题→加粗、`- 列表`→`• 列表`）。
- 飞书机器人可选安全设置「加签」：请求体附 `timestamp` + `sign`（HMAC-SHA256(timestamp\nsecret)，base64）。
- 响应成功判定：HTTP 200 且 `code == 0`（部分版本字段为 `StatusCode`）。
- 参考项目 [daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) 的 `feishu_sender.py` 已实现完整 webhook 路径，本 ADR 照其结构简化实现。

## 决策

1. **配置**（均走 GitHub Secrets，ADR-0009）：
   - `FEISHU_WEBHOOK_URL`：飞书机器人 webhook 链接（必填才启用飞书渠道）。
   - `FEISHU_WEBHOOK_SECRET`：可选，飞书机器人「加签」密钥；配置了才附加 `timestamp`/`sign`。
2. **渠道调度**：`send_messages(contents)` 遍历 `enabled_channels()`——企业微信（`WECHAT_WEBHOOK_URL`）与飞书（`FEISHU_WEBHOOK_URL`）各自独立判断，有哪个推哪个，都有都推；单渠道失败不影响另一渠道；dry-run 时打印「渠道: 企业微信、飞书」。
3. **启动检查**：两个 webhook 都未配置且非 dry-run → 报错退出（防止用户误以为在推送）。
4. **飞书消息格式**：`interactive` 卡片优先——首行 `## xxx` 提取为卡片 `header`（plain_text），正文经 `format_feishu_markdown` 转换后放入 `lark_md` 元素；卡片发送失败降级为 `text` 纯文本（信息不丢）。
5. **内容与拆分**：复用现有消息内容与 `pack_signal_messages` 拆分（≤3800 字节），飞书卡片容量远大于此，无需单独阈值。
6. 企业微信渠道行为不变（ADR-0007 markdown；`errcode == 0` 判定成功）。

## 后果

- 用户可只配飞书（当前实际使用），企业微信能力保留可随时启用。
- 同一消息在两个渠道各推一条，内容一致（飞书以卡片渲染）。
- 飞书限流（100 条/分钟）远高于本项目 16 条/天，无压力。
