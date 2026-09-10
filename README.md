# beancounter_telegram

通过 Telegram 记账、查询和分析 GitHub 上的 Beancount 账本，支持 Beancount v2 / v3。

## 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n config.json.example config.json  # 仅首次部署需要复制配置
# 编辑 config.json 后再启动
python main.py
```

启动前填写 `config.json`；已有配置请勿覆盖：

| 配置 | 用途 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | [BotFather](https://t.me/BotFather) 创建的机器人 |
| `CHAT_ID` | 允许访问的聊天 ID，必填；多个用逗号分隔 |
| `GITHUB_TOKEN` | 目标账本仓库的读写权限 |
| `REPO_OWNER` / `REPO_NAME` / `BRANCH_NAME` | 账本仓库和分支 |
| `FILE_PATH` | 交易写入文件 |
| `LEDGER_ROOT` | 完整账本入口，必须 include 写入文件；默认优先 `main.bean` |
| `TIMEZONE` | 如 `Asia/Shanghai` 或 `Europe/London` |
| `LLM_BACKENDS` | 可选的 OpenAI Chat Completions 兼容后端，按顺序故障切换 |

LLM 的地址、密钥和模型配置见 [配置模板](config.json.example)。截图可单独指定 `LLM_VISION_MODEL`。账户定义放在账本仓库的 `accounts/` 及其子目录。

## 使用

- 记账：发送 `星巴克 35 CNY，支付宝支付`，或发送账单截图。信息不足时会要求补充。
- 查询：发送 `这个月吃饭花了多少`、`招行余额` 或 `最近 10 条 Chase 记录`，直接返回表格。
- 分析：发送 `分析最近三个月消费趋势`、`为什么这个月比上个月花得多`。模型可连续查询，再给出带查询来源的解释和代码块表格。
- 草稿：✅ 保存、🔧 修改、❌ 丢弃。默认 **120 秒无操作自动确认**；修改或检查失败后暂停自动确认，可用 `DRAFT_TTL_SECONDS` 调整时长。

LLM 草稿保存前必须通过完整账本校验和独立 LLM 一致性审核。保存交易后自动展示**当前自然月分类开支**及**本笔涉及的资产／负债账户余额**，表格省略冗余账户前缀。开支按成本分币种统计，余额包含全部账本日期、保留持仓单位和负债符号；统计失败不影响已保存交易。

### 分析边界

分析按需探测后端模型的工具调用及结果回传能力，不需要开关。明确支持或不支持的结果缓存一小时；不支持时自动使用 JSON 查询协议。探测结果不明确、探测超时或预算耗尽时，仅本次使用 JSON，不缓存为不支持。鉴权、限流及其他连接错误按顺序尝试下一后端。

每次分析只加载一次完整账本，最多 4 轮查询、8 条 BQL，再进行一次总结。仅开放只读 SELECT，每次返回最多 100 行，过长结果明确标注截断。每条 BQL 的结果等待上限仍为 15 秒，超时后终止查询进程。模型解释仍需核对，不能将其推测当作事实。

以下配置只影响多步分析，单位为秒；已有配置未填写时使用默认值：

| 配置 | 默认值 | 用途 |
| --- | ---: | --- |
| `ANALYSIS_REQUEST_TIMEOUT_SECONDS` | 180 | 单次分析／探测 HTTP 连接和读取超时，受剩余预算约束 |
| `ANALYSIS_TIMEOUT_SECONDS` | 600 | 账本加载后的整个分析流程预算 |
| `ANALYSIS_PROBE_BUDGET_SECONDS` | 120 | 所有后端共享的能力探测预算 |

这些预算不构成严格的端到端墙钟保证：HTTP 超时不是总下载时限，进程启动、传输和清理也有开销。整体预算耗尽时返回已取得的结果，不修改账本。分析失败日志保留 HTTP 状态码及限长的错误类型、代码和消息，并遮盖已配置的密钥；日志仍可能包含私人交易信息，请勿公开。

### 常用命令

Bot 启动时自动为 `CHAT_ID` 中的会话注册中文 `/` 命令菜单，并为私聊设置菜单按钮，无需在 BotFather 手动录入。注册失败会记录日志，每隔约 5 分钟重试，期间继续处理消息；重启会重新同步菜单。输入 `/help` 可查看命令和记账示例，群聊支持 `/命令@机器人用户名`。

| 输入 | 操作 |
| --- | --- |
| `/start` / `/help` | 入门说明、命令用法和记账示例 |
| `/last [N]` / `/today` | 最近 N 条（默认 5）／今天的记录 |
| `/undo` | 预览并确认撤回最后一条指令 |
| `/tz` / `/tz Europe/London` | 查看／设置时区 |
| `open Assets:Cash GBP` / `close Assets:Cash` | 开户／销户 |
| `balance Cash 200 GBP` | 余额断言，默认次日开盘生效 |
| `pad Cash Opening-Balances` | 补差指令 |
| `/update Cash Opening-Balances 200 GBP` | 今天 pad，明天 balance |
| `/view` | 触发账本仓库的月度 Sankey 工作流 |

多行文本走手动记账，可用账户后缀匹配；日期、tag 和 link 可选：

```text
2026-09-10
KFC
午餐
Food 20 GBP
Cash -20 GBP
```

## 个性化与运维

- 将 [user.md.example](user.md.example) 复制为本地 `user.md`，填写账户别名、分类和语言偏好；每次业务 LLM 请求重新读取，无需重启。已有文件请勿覆盖。
- `config.json`、`user.md` 和 `data/` 不提交到 Git。更新前停止旧实例并备份它们，保留 SQLite 状态，不要同时运行两个实例。旧版本若仍跟踪 `user.md`，先备份到仓库外，更新后再恢复。
- 用户输入、账户信息、必要查询结果和偏好会发送到配置的 LLM 服务；分析不开放交易元数据查询。日志包含模型返回和交易信息，请限制访问。
- 可选工作流模板见 [.github/workflows](.github/workflows)：复制到**账本仓库**并去掉 `.example`，设置 `TELEGRAM_TOKEN` 和 `TELEGRAM_CHAT_ID` secrets。`/view` 需要 `monthly-report.yml`；`notify-on-push.yml` 可能与保存后的统计重复通知。
- [依赖与升级](requirements.md) · [开发与测试](CLAUDE.md) · [完整校验性能基准](scripts/benchmark_ledger.py)
