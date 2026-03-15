# beancounter_telegram

追加记账记录到特定 GitHub 仓库的特定文件，随时随地用 Telegram 也可以记录生活消费，且不影响现有的 Beancount 工作流。

## 部署方式

本项目支持两种部署方式，功能相同：

| | Python Bot | Cloudflare Worker |
|---|---|---|
| 目录 | 根目录 | `worker/` |
| 运行方式 | 长轮询（polling） | Webhook（无服务器） |
| 状态存储 | 内存 | Cloudflare KV |
| LLM 配置 | `config.json` 中 `LLM_BACKENDS` 数组 | `wrangler secret` 单后端 |

## Quick Start（Python Bot）

- 依赖

  ```bash
  pip install -r requirements.txt
  ```

- 配置
  - 复制 `config.json.example` 为 `config.json`，填写以下字段：
    - `TELEGRAM_BOT_TOKEN`：通过 [BotFather](https://core.telegram.org/bots) 创建机器人并获取
    - `GITHUB_TOKEN`、`REPO_OWNER`、`REPO_NAME`、`BRANCH_NAME`、`FILE_PATH`：目标仓库信息
    - `CHAT_ID`：向机器人发一条消息后访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 获取
    - `TIMEZONE`：时区，如 `Asia/Shanghai`、`Europe/London`
    - `LLM_BACKENDS`：兼容 OpenAI API 的 LLM 后端列表，用于自然语言记账（可选）。按顺序尝试，前一个失败自动 fallback 到下一个：
      ```json
      "LLM_BACKENDS": [
          { "LLM_API_BASE_URL": "https://api.openai.com/v1", "LLM_API_KEY": "sk-...", "LLM_MODEL": "gpt-4o-mini" },
          { "LLM_API_BASE_URL": "https://api.example.com/v1", "LLM_API_KEY": "sk-...", "LLM_MODEL": "gpt-4o" }
      ]
      ```

- 运行
  ```bash
  python main.py
  ```

## Quick Start（Cloudflare Worker）

详见 [`worker/README.md`](worker/README.md)。简要步骤：

1. 配置 `worker/wrangler.toml` 中的基本变量（`REPO_OWNER`、`REPO_NAME` 等）
2. 设置 secrets：`TELEGRAM_BOT_TOKEN`、`GITHUB_TOKEN`、`WEBHOOK_SECRET`、`LLM_API_KEY`、`LLM_API_BASE_URL`、`LLM_MODEL`
3. 创建 KV 命名空间：`wrangler kv namespace create KV`，将 ID 填入 `wrangler.toml`
4. 部署：`cd worker && npm install && wrangler deploy`
5. 注册 Webhook：
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<worker-url>.workers.dev&secret_token=<WEBHOOK_SECRET>"
   ```

## 功能

- [x] `open`、`close`、`balance`、`pad` 指令
- [x] `/update [account] [account for pad] [amount] [currency]`：修正账户余额，今天插入 `pad`，明天插入 `balance`
- [x] 手动记账，根据后缀自动匹配对应账户（账户列表自动从仓库 `/accounts/*.bean` 中解析 `open` 指令获取）
- [x] `/tz <timezone>` 设置时区
- [x] **自然语言记账（LLM）**：单行输入自动调用 LLM 生成 beancount 条目，支持审核、重新生成、反馈修正
- [x] `/view` 触发当月 Sankey 图生成（调用账本仓库的 `monthly-report.yml` workflow）
- [x] `/undo` 撤回 `main.bean` 中的最后一条指令（支持 transaction、balance、pad、open、close 等任意顶层指令）
- [x] `/last [N]` 查看 `main.bean` 中最近 N 条记录（默认 5 条）
- [x] `/today` 查看今天的所有记录（根据 bot 时区判断）

## 账本仓库 GitHub Actions

`.github/workflows/` 下提供了两个可选的 workflow 示例文件，需复制到**账本仓库**（即 `REPO_NAME` 所指向的仓库）并去掉 `.example` 后缀后使用。

在账本仓库的 **Settings → Secrets and variables → Actions** 中配置以下 secrets：

| Secret | 说明 |
|--------|------|
| `TELEGRAM_TOKEN` | Telegram Bot Token |
| `TELEGRAM_CHAT_ID` | 接收通知的 Chat ID |

### monthly-report.yml

每天 08:00 UTC 自动运行，查询当月 `Expenses:*` 账户支出，生成 Sankey 图并发送到 Telegram。也可通过 `/view` 指令或手动 `workflow_dispatch` 触发，支持传入 `year_month`（`YYYY-MM`）指定月份。

### notify-on-push.yml

每次 push 到 `main` 分支时触发，发送两条通知：

1. 当月各 `Expenses` 子账户明细及总计
2. 本次 commit message body 中列出的账户的当前余额

## LLM 自然语言记账

发送一行自然语言描述，机器人会调用 LLM 生成草稿并发送审核按钮：

| 按钮 | 操作 |
|------|------|
| ✅ | 保存到仓库 |
| 🔧 | 输入反馈后重新生成 |
| ❌ | 丢弃 |

**支持的输入示例：**

```beancount
; 微信消费 5 块（微信账户开户时声明了 CNY，自动推断货币）
YYYY-MM-DD * "商家" "餐饮"
  Assets:WeChat:Current     -5 CNY
  Expenses:Food              5 CNY

; KFC 花了 20 USD 微信支付
YYYY-MM-DD * "KFC" "餐饮"
  Assets:Bank:WeChat     -20 USD
  Expenses:Food           20 USD

; 和 John Wick 吃晚餐萨莉亚 96 GBP，刷的 chase 信用卡，他给我 48 GBP 现金
YYYY-MM-DD * "萨莉亚" "晚餐"
  Liabilities:CreditCard:Chase     -96 GBP
  Assets:Cash                       48 GBP
  Expenses:Food                     48 GBP

; 支付宝买了杯咖啡 35 CNY
YYYY-MM-DD * "咖啡店" "咖啡"
  Assets:Bank:Alipay:Current     -35 CNY
  Expenses:Food                   35 CNY
```

**规则说明：**
- 单行文本自动走 LLM 流程；多行文本走手动记账流程
- 输入里需要至少暗示扣款账户（如 `微信` / `支付宝` / `现金` / `HSBC`）；信息不足时机器人会提示补充
- 未说明货币时，优先使用扣款账户在 `open` 指令中声明的默认货币（如 `open Assets:WeChat:Current CNY` → 默认 CNY）；若账户无默认货币且全文只出现一种货币则以此为默认
- 未说明支付方式时，默认使用微信/支付宝余额账户（非理财子账户）
- 分摊消费：付全款、他人转账回来的金额从支付账户正向抵消，`Expenses` 仅记录自己的净份额
- 人名默认首字母大写；narration 默认中文（英文输入时用英文）

# Example

- open

  ```
  open Assets:Bank:HSBC:Current GBP
  ```

- close

  ```
  close Assets:Bank:HSBC:Current
  ```

- balance

  默认日期为**次日**（beancount balance 断言在所述日期的开盘时生效，因此填次日表示"今日收盘后余额"）。如需指定日期，在消息第一行写 `YYYY-MM-DD`。

  ```
  balance Alipay 200 CNY
  ```

- pad

  ```
  pad Alipay Opening-Balances
  ```

- 手动记账：date、link 和 tag 可选。

  ```
  KFC
  玩原神玩的
  ^testlink
  #taggggg
  Food 20 CNY
  WeChat -20 CNY
  ```

- 手动记账：当没有 payee，仅 narration 时，填写 payee，将 narration 留空。

  ```
  test only narration

  HSBC:current 200 GBP
  assets:cash -200 GBP
  ```

- 设置时区

  ```
  /tz Asia/Shanghai
  ```

- update（今天插入 pad，明天插入 balance）

  ```
  /update Alipay Food 200 CNY
  ```

- 触发当月 Sankey 报告生成

  ```
  /view
  ```

- 撤回最后一条指令（预览后确认）

  ```
  /undo
  ```

- 查看最近 5 条记录（默认），或指定数量

  ```
  /last
  /last 10
  ```

- 查看今天的所有记录

  ```
  /today
  ```
