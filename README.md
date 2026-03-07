# beancounter_telegram

追加记账记录到特定 GitHub 仓库的特定文件，随时随地用 Telegram 也可以记录生活消费，且不影响现有的 Beancount 工作流。

## Quick Start

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

## 功能

- [x] `open`、`balance`、`pad` 指令
- [x] `/update [account] [account for pad] [amount] [currency]`：修正账户余额，今天插入 `pad`，明天插入 `balance`
- [x] 手动记账，根据后缀自动匹配对应账户（账户列表自动从仓库 `/accounts/*.bean` 中解析 `open` 指令获取）
- [x] `/tz <timezone>` 设置时区
- [x] **自然语言记账（LLM）**：单行输入自动调用 LLM 生成 beancount 条目，支持审核、重新生成、反馈修正
- [ ] `/view [date]` 查看指定日期的记账记录

## LLM 自然语言记账

发送一行自然语言描述，机器人会调用 LLM 生成草稿并发送审核按钮：

| 按钮 | 操作 |
|------|------|
| ✅ | 保存到仓库 |
| 🔧 | 输入反馈后重新生成 |
| ❌ | 丢弃 |

**支持的输入示例：**

```beancount
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
- 输入里需要至少暗示扣款账户（如 `微信` / `支付宝` / `现金` / `HSBC`）和货币（如 `CNY` / `GBP`）；信息不足时机器人会提示补充
- 未说明货币时，若全文只出现一种货币则以此为默认
- 未说明支付方式时，默认使用微信/支付宝余额账户（非理财子账户）
- 分摊消费：付全款、他人转账回来的金额从支付账户正向抵消，`Expenses` 仅记录自己的净份额
- 人名默认首字母大写；narration 默认中文（英文输入时用英文）

# Example

- open

  ```
  open Assets:Bank:HSBC:Current GBP
  ```

- balance

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
