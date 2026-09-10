# beancounter_telegram

通过 Telegram 记账、查询和分析 GitHub 上的 Beancount 账本，支持 Beancount v2 / v3。

## 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.json.example config.json
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

首次分析会自动探测各后端模型的工具调用及结果回传能力，不需要开关。明确支持或不支持的结果缓存一小时；不支持时自动使用 JSON 查询协议。返回普通文本而未调用工具时，本次使用 JSON，但不认定永久不支持；鉴权、限流和网络错误走后端故障切换，不缓存为不支持。

每次分析只加载一次完整账本，最多 4 轮查询、8 条 BQL，再进行一次总结。仅开放只读 SELECT，每次返回最多 100 行，过长结果明确标注截断。单条 BQL 超过 15 秒会终止查询进程；账本加载后的流程预算为 180 秒，HTTP 超时最多 60 秒（不是整次请求的严格墙钟上限）。预算耗尽时只返回已取得的结果，不修改账本。模型解释仍需核对，不能将其推测当作事实。

### 常用命令

| 输入 | 操作 |
| --- | --- |
| `/last [N]` / `/today` | 最近 N 条（默认 5）／今天的记录 |
| `/undo` | 预览并确认撤回最后一条指令 |
| `/tz Europe/London` | 设置时区 |
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
