---
name: daily-stock-analysis
description: 调用 daily_stock_analysis API 进行股票智能分析、横盘突破选股或获取指令菜单。当用户询问「分析茅台」「analyze AAPL」「帮我看看 600519」「dsa命令」「/menu」「横盘选股」「横盘突破」等时使用。
metadata:
  {"openclaw": {"requires": {"env": ["DSA_BASE_URL"]}, "primaryEnv": "DSA_BASE_URL"}}
---

## 触发条件

1. **股票分析**：当用户请求分析某只股票时（如「分析茅台」「analyze AAPL」「帮我看看 600519」）。
2. **指令菜单**：当用户询问 DSA 有哪些命令、怎么用、菜单等（如「/help」「/menu」「dsa命令」「有哪些命令」「指令列表」「功能菜单」）时，调用 `{DSA_BASE_URL}/api/v1/bot/commands` 返回指令清单。
3. **横盘突破选股**：当用户请求「横盘选股」「横盘突破」「盘整选股」等（意图是跑横盘突破策略并微信收结果）时，调用 `{DSA_BASE_URL}/api/v1/bot/<chat_id>/command` 下发 `screen` 命令（见下方「工作流程（横盘突破选股）」）。

## 工作流程（股票分析）

1. **提取股票代码**：从用户消息中识别股票代码（如 600519、AAPL、hk00700）。若用户仅提供中文名称（如「茅台」），需提示用户提供股票代码，或使用常见映射（茅台→600519）。
2. **调用 API**：向 `{DSA_BASE_URL}/api/v1/analysis/analyze` 发送 POST 请求，请求体：
   ```json
   {"stock_code": "<提取的代码>", "report_type": "detailed", "force_refresh": true, "async_mode": false, "skills": ["bull_trend"]}
   ```
   > `skills` 为可选策略 ID 数组；历史字段 `strategies` 仍保留兼容，建议优先使用 `skills`。
3. **等待响应**：同步模式下分析约需 2–5 分钟，请确保 HTTP 客户端超时足够（建议 ≥300 秒）。
4. **解析结果**：从响应的 `report.summary` 中提取 `operation_advice`、`trend_prediction`、`analysis_summary`，从 `report.strategy` 中提取 `ideal_buy`、`stop_loss`、`take_profit`，以简洁格式呈现给用户。外部集成可继续只读取自由文本 `operation_advice`；若需要结构化展示，可优先读取可选的 `action` / `action_label`（八态：`buy|add|hold|reduce|sell|watch|avoid|alert`）。旧历史缺字段时可回退到 `operation_advice` 文本展示，但该回退不等价于稳定 API action；旧三态统计口径仍以 `decision_type` 为准。
5. **错误处理**：
   - 连接失败：提示检查 DSA 是否运行、DSA_BASE_URL 是否正确
   - 400：检查 stock_code 格式
   - 409：该股票正在分析中，可稍后重试或查询任务状态
   - 500：提示查看 DSA 日志排查

## 工作流程（指令菜单）

1. **识别意图**：用户消息匹配 `/help`、`/menu`、`dsa命令`、`有哪些命令`、`指令列表`、`功能菜单` 等。
2. **调用 API**：向 `{DSA_BASE_URL}/api/v1/bot/commands` 发送 GET 请求。
3. **展示结果**：将响应中的 `markdown` 字段原样展示给用户（已按分组排版，可直接发送到微信）。
4. **错误处理**：连接失败时提示「检查 DSA 是否运行以及 DSA_BASE_URL」。

## 工作流程（横盘突破选股）

1. **识别意图**：用户消息匹配「横盘选股」「横盘突破」「盘整选股」等，或以 `横盘`/`sideways`/`consolidation` 开头。
2. **下发命令**：向 `{DSA_BASE_URL}/api/v1/bot/<chat_id>/command` 发送 POST 请求，请求体：
   ```json
   {"command": "/screen", "chat_id": "<对话ID>", "args": []}
   ```
   > 加 `--refresh`（或 `刷新`/`重选`）可强制重选；不加则直接复用当日已选缓存。
3. **即时回执**：该接口会立即返回「✅ 横盘突破选股已启动」提示（同步），随后后台跑 AlphaSift 横盘突破筛选（约 2–5 分钟）。
4. **结果推送**：选股完成后，DSA 会按原对话渠道（OpenClaw 微信）推送结果清单，无需轮询。
5. **错误处理**：若返回失败信息，提示「检查 DSA 是否运行以及 DSA_BASE_URL」。

## 股票代码格式

- A股：6位数字（600519、000001）
- 北交所：8/4/92 开头 6 位，支持 BJ 前缀或 .BJ 后缀（920748、BJ920493、920493.BJ）
- 港股：hk + 5位数字（hk00700）
- 美股：1–5 字母（AAPL、TSLA、BRK.B）
- 美股指数：SPX、DJI、IXIC 等
