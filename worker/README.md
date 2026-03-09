# Beancounter Telegram Bot Worker

Cloudflare Workers 部署版本，功能与 main.py 相同，支持自然语言 LLM 记账。

## 配置

1. 修改 `wrangler.toml` 中的基本配置：
   ```toml
   [vars]
   REPO_OWNER = "your-github-username"
   REPO_NAME = "your-repo-name"
   BRANCH_NAME = "main"
   FILE_PATH = "transactions.bean"
   TIMEZONE = "Asia/Shanghai"
   CHAT_ID = "your-telegram-chat-id"
   ```

2. 创建 KV 命名空间并更新 ID：
   ```bash
   wrangler kv:namespace create KV
   # 将返回的 ID 填入 wrangler.toml 的 kv_namespaces.id
   ```

3. 设置秘密环境变量：
   ```bash
   # Telegram Bot Token (from @BotFather)
   wrangler secret put TELEGRAM_BOT_TOKEN
   
   # GitHub Personal Access Token (需要 repo 权限)
   wrangler secret put GITHUB_TOKEN
   
   # Webhook 验证 Secret（自行生成一个随机字符串）
   wrangler secret put WEBHOOK_SECRET
   
   # LLM API 配置（兼容 OpenAI API 的服务）
   wrangler secret put LLM_API_KEY
   wrangler secret put LLM_API_BASE_URL
   wrangler secret put LLM_MODEL
   ```

   示例 LLM 配置：
   - OpenAI: `https://api.openai.com/v1`, model: `gpt-4o-mini`
   - 自托管 API: 填写你的 API base URL 和模型名

## 部署

```bash
cd worker
npm install
wrangler deploy
```

部署成功后会返回 Worker URL，例如 `https://beancounter-bot.your-subdomain.workers.dev`

## 注册 Webhook

部署完成后，向 Telegram 注册 webhook：

```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<worker-url>.workers.dev&secret_token=<WEBHOOK_SECRET>"
```

替换：
- `<TELEGRAM_BOT_TOKEN>`: 你的 Bot Token
- `<worker-url>`: Worker 的 URL
- `<WEBHOOK_SECRET>`: 你设置的 webhook secret

验证 webhook 状态：
```bash
curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

## 功能说明

### 自然语言记账（LLM）

发送一行文本描述，Worker 自动调用 LLM 生成 beancount 条目：

```
微信买了杯咖啡 35
```

LLM 会自动：
- 从仓库 `/accounts/*.bean` 解析可用账户
- 匹配支付方式到对应账户（微信支付 → WeChat:Current, 信用卡 → Liabilities:Card:* 等）
- 生成符合 beancount 格式的条目
- 直接保存到 GitHub（未来可添加审核流程）

### 手动记账

多行结构化输入（与 main.py 相同）：

```
KFC
早餐
Food 20 CNY
WeChat -20 CNY
```

### 命令

- `/tz <timezone>`: 设置时区（如 `Asia/Shanghai`, `Europe/London`）
- `open <account> <currency>`: 开户
- `balance <account_suffix> <amount> <currency>`: 余额检查
- `pad <account_suffix> <pad_account_suffix>`: 余额调整

### 账户匹配

Worker 会自动解析 GitHub 仓库中的账户，并缓存 5 分钟。输入时可以使用账户后缀简写：

- `WeChat` → `Assets:WeChat:Current`
- `Alipay` → `Assets:Alipay:Current`
- `Food` → `Expenses:Food`

## 开发

本地测试：
```bash
wrangler dev
```

同步账户列表（更新 `src/accounts.json`，仅用于本地测试）：
```bash
npm run sync-accounts
```

## 注意事项

1. **KV 缓存**: 账户列表缓存 300 秒，避免频繁调用 GitHub API
2. **LLM 配置**: 确保 LLM API 与 OpenAI API 兼容（需要 `/chat/completions` endpoint）
3. **GitHub Token**: 需要 `repo` 权限才能读写仓库文件
4. **时区**: 默认使用 `TIMEZONE` 变量，可通过 `/tz` 命令临时修改（保存在 KV 中）
