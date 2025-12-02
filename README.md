# beancounter_telegram

用 Telegram 来记账。

## Quick Start

- 依赖

  ```bash
  pip install -r requirements.txt
  ```

- 配置
  - 在 `config.yaml` 中填写 Telegram 机器人的 Token、Github 仓库信息和你的 Chat ID。
  - 你可以通过 [BotFather](https://core.telegram.org/bots) 创建一个新的 Telegram 机器人，并获取 Token。
  - 你可以通过向你的机器人发送一条消息，然后访问 `https://api.telegram.org/bot<YourBOTToken>/getUpdates` 来获取你的 Chat ID。
- 运行
  ```bash
  python main.py
  ```

## 功能

- [x] `open`
- [x] `balance`
- [x] 记账，根据后缀自动匹配对应账户
- [ ] 记账时自动补全货币
- [x] `/tz <timezone>` 设置时区
- [ ] `/view [date]` 查看指定日期的记账记录

# Example

- open

  ```
  open Assets:Bank:HSBC:Current GBP
  ```

- balance

  ```
  balance Alipay 200 CNY
  ```

- 记账：date、link 和 tag 可选。

  ```
  KFC
  玩原神玩的
  ^testlink
  #taggggg
  Food 20 CNY
  WeChat -20 CNY
  ```

- 记账：当没有 payee，仅 narration 时，填写 payee，将 narration 留空。

  ```
  test only narration

  HSBC:current 200 GBP
  assets:cash -200 GBP
  ```
- 设置时区

  ```
  /tz Asia/Shanghai
  ```