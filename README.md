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

## 项目结构

```text
main.py                 # 稳定启动入口：python main.py [debug]
beancounter/            # 业务代码包
  bot.py                # Bot 组装、消息和命令处理
  ledger.py             # 账本读取、账户及 GitHub 写入
  llm.py / prompts.py   # LLM 调用与提示构建
  drafts.py             # 草稿、确认与审核流程
  templates/            # Beancount 分录模板
  ...                   # 配置、校验、Telegram、调度和持久化模块
scripts/preview_llm.py   # 手动 LLM 预览工具，需显式 --live
tests/                  # 离线自动测试及测试数据规范
config.json.example     # 配置模板
user.md.example         # 用户 prompt 模板
```

个人 `config.json`、`user.md` 和运行状态 `data/` 留在项目根目录且不被 Git 跟踪。整理目录不改变它们的路径，也不改变 `python main.py` 的启动方式；模板随业务代码放在 `beancounter/templates/`，不依赖启动时的工作目录。Python 调用方使用 `from beancounter.bot import Bot`，原有 `from main import Bot` 仍可用。

## 功能

- [x] `open`、`close`、`balance`、`pad` 指令
- [x] `/update [account] [account for pad] [amount] [currency]`：修正账户余额，今天插入 `pad`，明天插入 `balance`
- [x] 手动记账，根据后缀自动匹配对应账户（账户列表自动从仓库 `/accounts/*.bean` 中解析 `open` 指令获取）
- [x] `/tz <timezone>` 设置时区
- [x] **自然语言记账（LLM）**：单行输入自动调用 LLM 生成 beancount 条目，支持审核、重新生成、反馈修正
- [x] **自然语言查询（LLM + BQL）**：直接提问即可检索账本，无需命令前缀。如「列出最近 10 条 chase 记录」「这个月吃饭花了多少」「各账户余额」。只读，不产生草稿
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

草稿成功发送后开始计时，默认 **120 秒无操作自动确认**（`config.json` 的
`DRAFT_TTL_SECONDS` 可调整；实际处理会受轮询间隔影响）。点击 🔧 后暂停自动确认，
修改后的新草稿重新计时。`/undo` 超时仍取消，不会自动撤回。

手动确认和超时确认均在提交前执行：

1. 下载同一 GitHub 版本的完整账本，将 journal 追加到本地临时副本，以 `LEDGER_ROOT`
   为入口运行与 `bean-check` 相同的校验（包括 `HARDCORE_VALIDATIONS`）。不配置时优先
   使用仓库中的 `main.bean`，否则使用 `FILE_PATH`；入口必须 include 写入的 journal。
2. 另发一次 LLM 审核请求，核对 journal 与原始输入、修改反馈和原图（截图记账）的
   金额、币种、账户、日期及交易含义是否一致。

两项均通过才保存。账本已有错误、下载不完整、审核拒绝或服务不可用时，保留草稿并暂停
自动确认，可点击 ✅ 重试或 🔧 修改。若 GitHub 提交响应丢失，重试会通过唯一操作标记
核对是否已经保存，避免重复追加。草稿、截图引用、修改反馈、时区及待处理消息保存到 SQLite，
重启后恢复。已进入修改或检查失败的草稿保持自动确认暂停；已发送且超时的草稿恢复后继续检查。

### 商家（payee）识别与模糊匹配

自然语言记账分两阶段：第一次 LLM 调用识别意图并提取 payee **检索线索**，第二次收到完整记账输入、匹配的历史分录和常用商家列表，生成最终分录。第一次的 payee不会直接锁定最终商家名，也不是程序随后通过固定规则补全名称。

- 历史检索忽略大小写，支持双向子串匹配。例如虚构商家 `Demo Cafe` 可以匹配 `Demo Cafe 42`，最多提供最近 10 条匹配分录。短名称也可能匹配多个不同商家。
- 生成阶段还会参考账本中前 50 个常用 payee；输入指向已有商家时，提示 LLM 沿用原名。
- 程序没有编辑距离等拼写纠错算法。`sainsburry` 通常不能直接匹配 `Sainsbury's`； 路由 LLM 可能先纠正拼写，或生成 LLM 根据原始输入和常用商家列表识别它，但不保证成功。
- 历史上下文加载失败时仍可生成草稿；保存前的完整账本校验和输入一致性审核不会跳过。

请检查草稿中的商家名；有误可点击 🔧 修正，也可在 `user.md` 中写入明确的商家别名偏好。日志中的 `Injecting past entries for payee ...` 表示检索线索，不代表最终 payee，也不列出具体匹配的商家；最终名称以生成阶段的 LLM 返回正文和草稿为准。

### 并发、状态与部署

- `WORKERS`：固定工作线程数，默认 4。同一聊天按消息顺序执行；超时确认也使用同一队列。
- `QUEUE_SIZE`：每条工作队列容量，默认 64。持久化收件箱总容量为两者乘积，满时停止推进
  Telegram 接收游标，等待处理后继续，不会无限创建线程。
- `STATE_PATH`：默认 `data/bot.sqlite3`，相对配置文件目录解析。请把该目录放在持久存储上，
  发布时保留。进程锁防止同一状态文件被两个实例同时使用；不同 bot/账本需使用不同文件。
- 导入 `main` 不读取配置；启动时加载 `config.json`。测试可显式传入 `Bot(settings=..., state_path=":memory:")`。
- 收件箱先落盘再确认接收，中断中的任务重启后重放。journal 写入使用稳定操作标记去重；
  生成请求、Telegram 通知或 `/view` 工作流在进程意外中断后可能再次执行。
- `SIGTERM` 会停止轮询并等待已排队任务结束。强制终止后仍可从 SQLite 恢复。

生产更新前先备份代码、`config.json`、`user.md` 和状态目录，再运行四套测试：

```bash
python tests/test_refactor.py
python tests/test_bot.py
python tests/test_fuzz.py
python tests/test_runtime.py
```

更换代码时保留配置和状态文件，首次升级前先处理完旧版本内存中的草稿。服务管理器应给
正常退出留足时间（例如 systemd 的 `TimeoutStopSec=300`）。回滚前停止新实例并备份 SQLite；
旧版本不识别新状态库，需先核对待处理草稿和账本，避免重复记账。

### 自定义提示：user.md

首次使用时，将 [`user.md.example`](user.md.example) 复制为项目根目录的 `user.md`（已有文件请勿覆盖），再写入账户别名、常用分类、语言偏好等。仓库只跟踪模板，个人 `user.md` 已加入 `.gitignore`，后续更新不会合并或覆盖它。模板本身不会被程序读取。

每次 LLM 调用都会重新读取 `user.md`，保存后无需重启，适用于意图路由、文本和截图记账、纠错重试及提交前审核。HTML 注释中的说明和示例不会发送；文件不存在或留空时不添加提示。自定义偏好不会跳过本地校验或改变审核要求。内容会发送给配置的 LLM 服务。

**旧版本首次迁移：请先把现有 `user.md` 备份到仓库外，再 pull，最后恢复为本地 `user.md`。** 本次更新会删除 Git 中原来跟踪的文件；未修改的副本可能随 pull 被移除，存在本地修改时 pull 可能被阻止。若被阻止，确认备份可用后再将旧跟踪文件恢复至当前提交的版本，然后重新 pull 并恢复备份。迁移完成后 `git ls-files -- user.md` 应无输出，`git check-ignore user.md` 应显示该文件。不要使用 `git add -f user.md`；取消跟踪不会清除旧 Git 历史。

例如（请替换为你的真实账户）：

```text
“工资”归入 Income:Salary。
咖啡消费归入 Expenses:Food:Coffee。
商家名称沿用原文，narration 使用中文。
```

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

### 诊断日志与隐私

每次 LLM 调用都会记录模型、用途、耗时及完整返回正文（包括路由、生成、重试和审核）。
请求开始、返回及失败日志均带有用途标签；首次路由调用负责意图识别、payee 提取及查询语句生成，后续分别标注文本/截图分录生成、用户反馈修改、校验纠错或保存前一致性审核。
本地 bean-check 失败时记录全部错误、文件名、行号和相关分录；生成阶段无法执行检查及保存失败也会记录原因。
日志可能包含私人交易和模型回显的输入，请限制日志访问，不要直接上传到公开 issue 或提交到 Git。
