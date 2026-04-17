interface LLMBackend {
	LLM_API_BASE_URL: string;
	LLM_API_KEY: string;
	LLM_MODEL: string;
}

interface Env {
	TELEGRAM_BOT_TOKEN: string;
	GITHUB_TOKEN: string;
	WEBHOOK_SECRET?: string;
	REPO_OWNER: string;
	REPO_NAME: string;
	BRANCH_NAME: string;
	FILE_PATH: string;
	TIMEZONE: string;
	CHAT_ID?: string;
	/** JSON array of LLMBackend objects */
	LLM_BACKENDS?: string;
	KV: KVNamespace;
}

interface Posting {
	account: string;
	amount: string;
	currency: string;
	rest: string;
	comment: string;
}

interface StoredPendingEntry {
	chatId: number;
	entryText: string;
	commitMessage: string;
	userInput: string;
	dateStr: string;
	createdAt: number;
}

// --- Constants ---

const ACCOUNTS_CACHE_TTL = 300; // 5 minutes
const ACCOUNTS_CACHE_KEY = 'accounts_cache';
const DRAFT_TTL_SECONDS = 120;

const ACCOUNT_TYPE_MAP: Record<string, string> = {
	assets: 'accounts/assets.bean',
	liabilities: 'accounts/liabilities.bean',
	equity: 'accounts/equity.bean',
	income: 'accounts/income.bean',
	expenses: 'accounts/expenses.bean',
};

// --- HTML escaping ---

export function escapeHtml(text: string): string {
	return text.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}

// --- Base64 (UTF-8 safe) ---

function encodeBase64(str: string): string {
	const bytes = new TextEncoder().encode(str);
	let binary = '';
	for (const byte of bytes) binary += String.fromCodePoint(byte);
	return btoa(binary);
}

function decodeBase64(b64: string): string {
	const binary = atob(b64.replaceAll(/\s/g, ''));
	const bytes = new Uint8Array(binary.length);
	for (let i = 0; i < binary.length; i++) bytes[i] = binary.codePointAt(i) ?? 0;
	return new TextDecoder().decode(bytes);
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
	const bytes = new Uint8Array(buffer);
	let binary = '';
	for (const byte of bytes) binary += String.fromCodePoint(byte);
	return btoa(binary);
}

// --- Date / Timezone ---

function formatInTimezone(tz: string): { dateStr: string; datetimeStr: string; timeStr: string } {
	const now = new Date();
	const parts = new Intl.DateTimeFormat('en-CA', {
		timeZone: tz,
		year: 'numeric',
		month: '2-digit',
		day: '2-digit',
		hour: '2-digit',
		minute: '2-digit',
		second: '2-digit',
		hour12: false,
		timeZoneName: 'longOffset',
	}).formatToParts(now);

	const get = (type: string) => parts.find((p) => p.type === type)?.value ?? '';
	const dateStr = `${get('year')}-${get('month')}-${get('day')}`;
	const tzOffset = get('timeZoneName').replace('GMT', '') || '+00:00';
	const datetimeStr = `${dateStr}T${get('hour')}:${get('minute')}:${get('second')}${tzOffset}`;
	const timeStr = `${get('hour')}:${get('minute')}`;
	return { dateStr, datetimeStr, timeStr };
}

export function addDays(dateStr: string, days: number): string {
	const d = new Date(dateStr + 'T00:00:00Z');
	d.setUTCDate(d.getUTCDate() + days);
	return d.toISOString().slice(0, 10);
}

function isValidTimezone(tz: string): boolean {
	try {
		Intl.DateTimeFormat(undefined, { timeZone: tz });
		return true;
	} catch {
		return false;
	}
}

async function getTimezoneForChat(env: Env, chatId: number): Promise<string> {
	return (await env.KV.get(`tz:${chatId}`)) ?? env.TIMEZONE;
}

// --- Telegram API ---

async function sendMessage(
	env: Env,
	chatId: number,
	text: string,
	options?: { parseMode?: 'Markdown' | 'HTML'; replyMarkup?: object },
): Promise<number | null> {
	const body: Record<string, unknown> = { chat_id: chatId, text };
	if (options?.parseMode) body.parse_mode = options.parseMode;
	if (options?.replyMarkup) body.reply_markup = options.replyMarkup;
	const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body),
	});
	if (r.ok) {
		const data = (await r.json()) as { result?: { message_id: number } };
		return data.result?.message_id ?? null;
	}
	return null;
}

async function editMessageReplyMarkup(env: Env, chatId: number, messageId: number): Promise<void> {
	await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/editMessageReplyMarkup`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ chat_id: chatId, message_id: messageId, reply_markup: { inline_keyboard: [] } }),
	});
}

async function answerCallbackQuery(env: Env, callbackQueryId: string, text?: string): Promise<void> {
	const body: Record<string, unknown> = { callback_query_id: callbackQueryId };
	if (text) body.text = text;
	await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body),
	});
}

async function getTelegramFileBytes(env: Env, fileId: string): Promise<ArrayBuffer | null> {
	const r = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/getFile?file_id=${encodeURIComponent(fileId)}`);
	if (!r.ok) return null;
	const data = (await r.json()) as { result?: { file_path: string } };
	if (!data.result?.file_path) return null;
	const dl = await fetch(`https://api.telegram.org/file/bot${env.TELEGRAM_BOT_TOKEN}/${data.result.file_path}`);
	if (!dl.ok) return null;
	return dl.arrayBuffer();
}

// --- GitHub API ---

function githubHeaders(env: Env): Record<string, string> {
	return {
		Authorization: `token ${env.GITHUB_TOKEN}`,
		Accept: 'application/vnd.github.object',
		'X-GitHub-Api-Version': '2022-11-28',
		'User-Agent': 'beancounter-worker',
	};
}

async function githubDownloadFile(env: Env, filePath?: string): Promise<{ content: string; sha: string } | null> {
	const path = filePath ?? env.FILE_PATH;
	const url = `https://api.github.com/repos/${env.REPO_OWNER}/${env.REPO_NAME}/contents/${path}?ref=${env.BRANCH_NAME}`;
	const r = await fetch(url, { headers: githubHeaders(env) });

	if (r.ok) {
		const data = (await r.json()) as { content: string; sha: string };
		return { content: decodeBase64(data.content), sha: data.sha };
	} else if (r.status === 404) {
		return { content: '', sha: '' };
	}
	return null;
}

async function githubUploadFile(env: Env, content: string, sha: string, commitMessage: string, filePath?: string): Promise<boolean> {
	const path = filePath ?? env.FILE_PATH;
	const url = `https://api.github.com/repos/${env.REPO_OWNER}/${env.REPO_NAME}/contents/${path}`;
	const r = await fetch(url, {
		method: 'PUT',
		headers: { ...githubHeaders(env), 'Content-Type': 'application/json' },
		body: JSON.stringify({
			message: commitMessage,
			content: encodeBase64(content),
			branch: env.BRANCH_NAME,
			...(sha ? { sha } : {}),
		}),
	});
	return r.status === 200 || r.status === 201;
}

async function githubTriggerWorkflow(env: Env, workflowFile: string, inputs: Record<string, string>): Promise<{ ok: boolean; error?: string }> {
	const url = `https://api.github.com/repos/${env.REPO_OWNER}/${env.REPO_NAME}/actions/workflows/${workflowFile}/dispatches`;
	const r = await fetch(url, {
		method: 'POST',
		headers: { ...githubHeaders(env), 'Content-Type': 'application/json' },
		body: JSON.stringify({ ref: env.BRANCH_NAME, inputs }),
	});
	if (r.status === 204) return { ok: true };
	const error = `${r.status} ${await r.text()}`;
	return { ok: false, error };
}

// --- Account parsing (with default currencies) ---

async function parseAccountsWithCurrencies(env: Env): Promise<{ accounts: string[]; currencies: Record<string, string>; comments: Record<string, string> }> {
	const cached = await env.KV.get(ACCOUNTS_CACHE_KEY, 'json');
	if (cached) {
		const { accounts, currencies, comments, timestamp } = cached as { accounts: string[]; currencies: Record<string, string>; comments: Record<string, string>; timestamp: number };
		if (Date.now() - timestamp < ACCOUNTS_CACHE_TTL * 1000) {
			return { accounts, currencies, comments: comments || {} };
		}
	}

	const url = `https://api.github.com/repos/${env.REPO_OWNER}/${env.REPO_NAME}/contents/accounts?ref=${env.BRANCH_NAME}`;
	const response = await fetch(url, {
		headers: {
			Authorization: `token ${env.GITHUB_TOKEN}`,
			Accept: 'application/vnd.github+json',
			'X-GitHub-Api-Version': '2022-11-28',
		},
	});

	if (!response.ok) {
		console.error('Error fetching accounts:', response.status);
		return { accounts: [], currencies: {}, comments: {} };
	}

	const files = (await response.json()) as Array<{ name: string; url: string }>;
	const beanFiles = files.filter((f) => f.name.endsWith('.bean'));

	const githubFileHeaders = {
		Authorization: `token ${env.GITHUB_TOKEN}`,
		Accept: 'application/vnd.github+json',
		'X-GitHub-Api-Version': '2022-11-28',
	};

	const fileContents = await Promise.all(
		beanFiles.map(async (file) => {
			const fileResponse = await fetch(file.url, { headers: githubFileHeaders });
			if (!fileResponse.ok) return null;
			const fileData = (await fileResponse.json()) as { content: string };
			return decodeBase64(fileData.content);
		}),
	);

	const opened: Record<string, { currency: string | null; comment: string | null }> = {};
	const closed = new Set<string>();

	for (const content of fileContents) {
		if (!content) continue;

		const openMatches = content.matchAll(/\d{4}-\d{2}-\d{2} open (\S+)(?:\s+([A-Z][A-Z0-9]{0,9}))?(.*)$/gm);
		for (const match of openMatches) {
			const account = match[1].trim();
			const currency = match[2] || null;
			const rest = match[3] || '';
			const comment = rest.includes(';') ? rest.split(';', 2)[1].trim() : null;
			opened[account] = { currency, comment };
		}

		const closeMatches = content.matchAll(/\d{4}-\d{2}-\d{2} close (\S+)/g);
		for (const match of closeMatches) {
			closed.add(match[1].trim());
		}
	}

	const currencies: Record<string, string> = {};
	const comments: Record<string, string> = {};
	const accounts: string[] = [];
	for (const [account, { currency, comment }] of Object.entries(opened)) {
		if (!closed.has(account)) {
			accounts.push(account);
			if (currency) currencies[account] = currency;
			if (comment) comments[account] = comment;
		}
	}
	accounts.sort();

	await env.KV.put(
		ACCOUNTS_CACHE_KEY,
		JSON.stringify({ accounts, currencies, comments, timestamp: Date.now() }),
		{ expirationTtl: ACCOUNTS_CACHE_TTL * 2 },
	);

	return { accounts, currencies, comments };
}

export function accountsForPrompt(accounts: string[], currencies: Record<string, string>, comments: Record<string, string> = {}): string[] {
	return accounts.map((a) => {
		let entry = a;
		if (currencies[a]) entry += ` (${currencies[a]})`;
		if (comments[a]) entry += ` ; ${comments[a]}`;
		return entry;
	});
}

// --- LLM Prompts ---

const BEANCOUNT_SYSTEM_PROMPT =
	'你是一个 Beancount 记账助手，将用户自然语言转换为一条 beancount 分录。\n\n' +
	'【账户规则】\n' +
	'- 只使用提供的账户列表中的账户，禁止自创账户或子账户。\n' +
	'- 账户列表可能包含注释：括号内为默认货币如 (CNY)，分号后为别名如 ; 招商银行。' +
	'这些注释仅供参考，生成 posting 时只写账户名，不要包含 (CNY) 或 ; 别名。\n' +
	'- 用 ; 别名 匹配用户提到的名称（如用户说「招商银行」→ 使用 Assets:Bank:CMB）。\n' +
	'- 当父账户和 :Current 子账户都存在时，优先使用 :Current。\n' +
	'- 如果用户输入中没有明确的账户信息，不要生成分录，' +
	'输出一行 NEED_ACCOUNT: 开头的纯文本说明缺少什么。\n\n' +
	'【支付方式映射】\n' +
	'- cash/现金 → 现金账户；微信/微信支付 → WeChat Pay 账户；支付宝 → Alipay 账户\n' +
	'- 信用卡/credit card/刷信用卡 → Liabilities 信用卡账户（不要用 Assets）\n' +
	'- 银行卡/debit card/刷卡（无「信用」）→ Assets:Bank:*:Current\n' +
	'- 未提及支付方式时，默认使用微信/支付宝的 :Current 或 :Balance 账户，' +
	'不要使用余额宝、基金等投资子账户。\n\n' +
	'【交易头部】\n' +
	'- payee 是商家/服务对象，不是支付渠道（如「微信充值原神」→ payee「原神」）。\n' +
	"- 订阅服务：payee 为平台名，narration 含具体套餐（如 payee 'ChatGPT', narration 'Pro 订阅'）。\n\n" +
	'【narration 规则】\n' +
	'- 用中文写，除非用户输入是英文。简洁 1-3 词，描述消费内容而非动作。\n' +
	"- 优先使用具体物品名（coke → 'Coke'，coffee → 'Coffee'），" +
	'仅在无具体物品时使用分类词（购物、餐饮、交通）。\n' +
	'- 不要用动词（吃、买、购买）作 narration。\n' +
	"- 用餐类型：brunch → 'Brunch'，lunch → 'Lunch'，dinner/晚餐 → '晚餐'。" +
	'如提供了当前时间且用户提到吃饭但未指定类型，按时间推断（早→早餐，中→午餐，晚→晚餐）。\n' +
	"- 中英文混排时加空格：'Pro 订阅'、'Netflix 会员'。\n\n" +
	'【人名】\n' +
	'- 英文人名首字母大写（john wick → John Wick）。\n' +
	'- 保留原始语言，不要音译（张三 保持 张三，不要写 Zhang San）。\n\n' +
	'【转账】\n' +
	'- 自有账户间转账：仅两条 posting（一正一负），不加 Expenses/Income。' +
	'无 payee 格式：YYYY-MM-DD * "转账"。\n' +
	'- 转给他人：payee 为收款人姓名，如 YYYY-MM-DD * "张三" "转账"。\n' +
	'- 不要在同一平台内生成自我转账（如 WeChat → WeChat）。\n\n' +
	'【分摊账单】\n' +
	'- 你付全款后他人转回各自份额：记为一笔平衡交易。' +
	'付款账户负全额，收款账户每人一条正 posting（行尾用 ; 标注人名），' +
	'Expenses = 全额 - 收回总额（即你的净份额）。交易必须归零。\n\n' +
	'【货币】\n' +
	"- 用户未指定货币时使用付款账户的默认货币。人民币用 CNY（非 RMB）。\n" +
	"- 跨币种交易用 @ 或 @@ 标注汇率。\n\n" +
	'【输出格式】\n' +
	'- 仅输出 beancount 文本，不要 markdown、不要解释。\n' +
	'- 每笔交易所有 posting 金额之和必须为零。\n' +
	'- 简单交易通常两条 posting（一正一负）；' +
	'多付款来源时需要多条 posting（如一条 Expenses 正值 + 多条付款负值）。\n\n' +
	'示例 1（单一付款）：\n' +
	'YYYY-MM-DD * "商家" "描述"\n' +
	'  Expenses:Category  10 USD\n' +
	'  Assets:Bank:Current  -10 USD\n\n' +
	'示例 2（多付款来源）：\n' +
	'YYYY-MM-DD * "商家" "描述"\n' +
	'  Expenses:Category  10 USD\n' +
	'  Liabilities:CreditCard  -6 USD\n' +
	'  Assets:GiftCard  -4 USD\n';

const INVEST_ORDER_SYSTEM_PROMPT =
	'你是一个 Beancount 投资订单助手，分析截图生成一条 beancount 交易。\n' +
	'只使用提供的账户列表中的账户。\n\n' +
	'【买入】\n' +
	'- 持仓账户用 @@ 总成本记法（不含手续费）：QUANTITY TICKER @@ TOTAL_COST CURRENCY ; @ PRICE_PER_SHARE PRICE_CURRENCY\n' +
	"- 单价写在行尾注释（; 后），不要用花括号 {} 成本记法。\n" +
	'- 如有手续费，单独一条 Expenses posting。\n' +
	'- 现金账户负金额为总支付额（含手续费）。\n\n' +
	'【卖出】\n' +
	'- 现金账户正金额为净收入。\n' +
	'- 资本损益：Income 账户，盈利为负值，亏损为正值。\n' +
	'- 持仓账户：-QUANTITY TICKER @@（@@ 后不写金额，beancount 自动计算成本）。\n' +
	'- 如有手续费，单独一条 Expenses posting。\n\n' +
	'【账户选择】\n' +
	'- 同一券商可能有多个子账户（如 Trading212 的 Stocks ISA 和 Invest）。\n' +
	'- 用户会在 caption 中用关键词指定账户类型（如 stocksisa、isa、invest、cfd）。\n' +
	'- 根据 caption 关键词匹配账户列表中对应的子账户' +
	'（如 caption 含 stocksisa 或 isa → 使用含 StocksISA 的账户；' +
	'caption 含 invest → 使用含 Invest 的账户）。\n' +
	'- 现金账户和持仓账户必须属于同一子账户。\n\n' +
	'【通用规则】\n' +
	'- 日期：用截图中的成交日期，非提交日期。\n' +
	"- payee：券商名称（如 Trading 212、IBKR）。\n" +
	"- narration：'Buy/Sell QUANTITY TICKER (公司全名)'。\n" +
	'- 截图未显示手续费则不要编造。\n' +
	'- 仅输出 beancount 文本，不要 markdown、不要解释。\n\n' +
	'买入示例：\n' +
	'2026-03-06 * "Trading 212" "Buy 15.5 GOOGL (Google)"\n' +
	'  Assets:Broker:GOOGL      15.5 GOOGL @@ 3464.78 GBP  ; @ 297.75 USD\n' +
	'  Expenses:Investments:Fee   5.20 GBP\n' +
	'  Assets:Broker:Cash       -3469.98 GBP\n' +
	'\n' +
	'卖出示例（收益 266.98 GBP，净收入 2406.54 GBP）：\n' +
	'2026-03-06 * "Trading 212" "Sell 23 ANET (Arista Networks)"\n' +
	'  Assets:Broker:Cash        2406.54 GBP\n' +
	'  Income:Broker:CapitalGains  -266.98 GBP\n' +
	'  Assets:Broker:ANET           -23 ANET   @@\n';

export function buildUserPrompt(
	txnDate: string,
	accountsWithCurrencies: string[],
	userInput: string,
	previousDraft?: string,
	declineReason?: string,
	currentTime?: string,
): string {
	const timeInfo = currentTime ? ` (current time: ${currentTime})` : '';
	let prompt =
		`Transaction date is ${txnDate}. Use this exact date in the output.${timeInfo}\n` +
		'Account list:\n' +
		accountsWithCurrencies.join('\n') +
		'\n\n' +
		`User input: ${userInput}\n`;

	if (previousDraft) prompt += `Previous declined draft:\n${previousDraft}\n\n`;
	if (declineReason) prompt += `Decline reason from user:\n${declineReason}\n\n`;

	prompt += 'Generate a valid, balanced beancount transaction.';
	return prompt;
}

const EXPENSE_SCREENSHOT_SYSTEM_PROMPT =
	'你是一个 Beancount 消费截图助手，分析消费通知截图（银行推送、信用卡提醒、支付确认）生成一条 beancount 交易。\n' +
	'只使用提供的账户列表中的账户，禁止自创账户。\n\n' +
	'从截图中提取：商家名称、金额和货币、支付来源（银行/���名）。\n' +
	'将支付来源映射到账户列表中最匹配的 Liabilities（信用卡）或 Assets（借记卡/银行）账户。\n' +
	'选择最匹配商家类型的 Expenses 账户。\n\n' +
	'narration：如果用户附带了 caption 消息，用 caption 作为 narration；' +
	'否则从商家名称推断简洁 narration（1-3 词，中文优先）。\n\n' +
	'【时间】\n' +
	'- 日期：默认使用提供的日期，除非截图中明确显示不同日期。\n' +
	'- 如果截图是 iOS 通知且显示相对时间（如 "5m ago"、"2h ago"），' +
	'基于 prompt 中提供的 current datetime 推算实际交易时间，' +
	'并在分录头部之后插入 datetime 元数据（ISO 8601 格式）：\n' +
	'    datetime: "YYYY-MM-DDTHH:MM:SS±HH:MM"\n' +
	'- 无法识别相对时间时不要输出 datetime 元数据。\n\n' +
	'仅输出 beancount 文本，不要 markdown、不要解释。\n\n' +
	"示例（Chase 信用卡通知，Sainsbury's 消费，caption: '买菜'）：\n" +
	'2026-04-06 * "Sainsbury\'s" "买菜"\n' +
	'  Expenses:Food                    5.50 GBP\n' +
	'  Liabilities:CreditCard:Chase    -5.50 GBP\n';

function buildExpenseScreenshotPrompt(txnDate: string, accountsWithCurrencies: string[], caption?: string, currentDatetime?: string): string {
	const timeInfo = currentDatetime ? ` (current datetime: ${currentDatetime})` : '';
	let prompt =
		`Transaction date is ${txnDate}. Use this exact date in the output.${timeInfo}\n` +
		'Account list:\n' +
		accountsWithCurrencies.join('\n') +
		'\n\nAnalyze the expense notification screenshot and generate the beancount transaction.';
	if (caption) prompt += `\nUser caption (use as narration context): ${caption}`;
	return prompt;
}

function buildInvestOrderPrompt(txnDate: string, accountsWithCurrencies: string[], caption?: string, currentDatetime?: string): string {
	const timeInfo = currentDatetime ? ` (current datetime: ${currentDatetime})` : '';
	let prompt =
		`Reference date (today): ${txnDate}${timeInfo}.\n` +
		'Account list:\n' +
		accountsWithCurrencies.join('\n') +
		'\n\nAnalyze the investment order screenshot and generate the beancount transaction. ' +
		'Use the fill/execution date shown in the screenshot as the transaction date.';
	if (caption) prompt += `\n用户 caption（根据关键词选择对应子账户）：${caption}`;
	return prompt;
}

// --- LLM functions ---

export function getLLMBackends(env: Env): LLMBackend[] {
	if (env.LLM_BACKENDS) {
		try {
			const parsed = JSON.parse(env.LLM_BACKENDS) as LLMBackend[];
			return parsed.filter((b) => b.LLM_API_BASE_URL && b.LLM_API_KEY && b.LLM_MODEL);
		} catch {
			// invalid JSON
		}
	}
	return [];
}

async function callLLMRaw(env: Env, messages: unknown[], temperature: number): Promise<string> {
	const backends = getLLMBackends(env);
	if (backends.length === 0) {
		throw new Error('LLM is not configured. Please set LLM_BACKENDS.');
	}

	let lastError: Error | null = null;
	for (const backend of backends) {
		try {
			const url = `${backend.LLM_API_BASE_URL.replace(/\/$/, '')}/chat/completions`;
			const response = await fetch(url, {
				method: 'POST',
				headers: {
					Authorization: `Bearer ${backend.LLM_API_KEY}`,
					'Content-Type': 'application/json',
				},
				body: JSON.stringify({ model: backend.LLM_MODEL, temperature, messages }),
			});

			if (!response.ok) {
				throw new Error(`LLM API error: ${response.status} ${response.statusText}`);
			}

			const data = (await response.json()) as { choices?: Array<{ message?: { content?: string } }> };
			const content = data.choices?.[0]?.message?.content;
			if (!content) {
				throw new Error(`Invalid LLM response: no content in choices`);
			}
			const rawText = content.trim();

			if (rawText.toUpperCase().startsWith('NEED_ACCOUNT:')) {
				const guidance = rawText.includes(':') ? rawText.split(':', 2)[1].trim() : '';
				throw Object.assign(new Error(guidance || '请在输入中提供至少一个账户名（或账户后缀）'), { isUserError: true });
			}

			return rawText;
		} catch (e) {
			const err = e instanceof Error ? e : new Error(String(e));
			if ((err as { isUserError?: boolean }).isUserError) throw err;
			console.error(`LLM backend '${backend.LLM_MODEL}' failed: ${err.message}, trying next...`);
			lastError = err;
		}
	}
	throw new Error(`All LLM backends failed. Last error: ${lastError?.message}`);
}

export function preferCurrentAccount(account: string, accounts: string[]): string {
	const accountsLower = new Map(accounts.map((a) => [a.toLowerCase(), a]));

	const lower = account.toLowerCase();
	if (accountsLower.has(lower)) return accountsLower.get(lower)!;

	if (!lower.startsWith('liabilities:') && !lower.includes(':current')) {
		const candidate = `${account}:Current`;
		if (accountsLower.has(candidate.toLowerCase())) return accountsLower.get(candidate.toLowerCase())!;
	}

	return account;
}

export function stripCodeFence(text: string): string {
	const stripped = text.trim();
	// Remove code fence markers (```lang or ```) but only the marker line itself
	const cleaned = stripped
		.split('\n')
		.filter((line) => !/^[ \t]*```\w*[ \t]*$/.test(line))
		.join('\n')
		.trim();
	// Extract beancount entry: find the transaction header and collect from there
	const headerRe = /^[ \t]*\d{4}-\d{2}-\d{2}\s+[*!txn]/m;
	const m = headerRe.exec(cleaned);
	if (m) {
		// Collect leading ; comment lines immediately before the header
		const before = cleaned.slice(0, m.index);
		const comments: string[] = [];
		const beforeLines = before.split('\n');
		for (let i = beforeLines.length - 1; i >= 0; i--) {
			const line = beforeLines[i];
			if (line.trim().startsWith(';')) {
				comments.unshift(line);
			} else if (line.trim() === '') {
				continue;
			} else {
				break;
			}
		}
		// Collect header + all subsequent indented/posting/comment lines
		const after = cleaned.slice(m.index);
		const afterLines = after.split('\n');
		const entryLines: string[] = [];
		for (let i = 0; i < afterLines.length; i++) {
			const line = afterLines[i];
			if (i === 0) {
				entryLines.push(line.trim());
			} else if (line.trim() === '' || line[0] === ' ' || line[0] === '\t' || line.trim().startsWith(';')) {
				entryLines.push(line);
			} else {
				break;
			}
		}
		return [...comments, ...entryLines].join('\n').trim();
	}
	return cleaned;
}

export function normalizeAndValidateLLMEntry(entryText: string, accounts: string[]): string {
	const text = stripCodeFence(entryText);
	const rawLines = text
		.split('\n')
		.map((l) => l.trimEnd())
		.filter((l) => l.trim());

	if (rawLines.length < 3) {
		throw new Error('LLM output is too short. Expected a transaction header and at least two postings.');
	}

	// Skip leading ; comment lines to find the header
	let headerIdx = 0;
	const leadingComments: string[] = [];
	for (let i = 0; i < rawLines.length; i++) {
		if (rawLines[i].trim().startsWith(';')) {
			leadingComments.push(rawLines[i].trim());
		} else {
			headerIdx = i;
			break;
		}
	}

	const header = rawLines[headerIdx].trim();
	// Validate header looks like a beancount directive (YYYY-MM-DD ...)
	if (!/^\d{4}-\d{2}-\d{2}\s+/.test(header)) {
		throw new Error(`LLM output invalid: first line is not a beancount directive header: '${header}'`);
	}

	const metadataLines: string[] = leadingComments.map((c) => (c.startsWith('  ') ? c : `  ${c}`));
	const postings: Array<{ account: string; amount: string; currency: string; rest: string }> = [];
	const postingRe = /^\s*(\S+)\s+(-?\d+(?:\.\d+)?)\s+(\S+)(?:\s+(.*))?$/;
	// Beancount metadata: key-value (e.g. "  key: value") or inline comments ("; ...")
	const metadataRe = /^\s*(\w[\w-]*\s*:.*|;.*)$/;
	// Strip parenthesized currency/alias annotations that LLMs sometimes copy
	const parenAnnotationRe = /\s+\([^)]*\)(?=\s)/g;

	for (let line of rawLines.slice(headerIdx + 1)) {
		line = line.replace(parenAnnotationRe, '');
		const pm = postingRe.exec(line);
		if (pm) {
			postings.push({
				account: preferCurrentAccount(pm[1], accounts),
				amount: pm[2],
				currency: pm[3],
				rest: (pm[4] || '').trim(),
			});
		} else if (metadataRe.test(line)) {
			// Only keep valid beancount metadata/comment lines; skip natural language
			metadataLines.push(`  ${line.trim()}`);
		}
	}

	if (postings.length < 2) {
		throw new Error('LLM output must contain at least two postings.');
	}

	const currencies = new Set(postings.map((p) => p.currency));
	if (currencies.size === 1) {
		const total = postings.reduce((sum, p) => sum + Number.parseFloat(p.amount), 0);
		if (Math.abs(total) > 0.0001) {
			throw new Error(`LLM output invalid: postings do not balance (sum = ${total.toFixed(4)}).`);
		}
	}

	if (postings.length === 2) {
		const a0 = Number.parseFloat(postings[0].amount);
		const a1 = Number.parseFloat(postings[1].amount);
		const c0 = postings[0].currency;
		const c1 = postings[1].currency;

		if (a0 * a1 >= 0) {
			throw new Error('LLM output invalid: two postings must be one positive and one negative.');
		}

		if (c0 === c1 && Math.abs(a0 + a1) > 0.0001) {
			throw new Error(`LLM output invalid: same-currency postings are unbalanced (${a0} + ${a1} != 0).`);
		}

		if (c0 !== c1) {
			const r0 = postings[0].rest;
			const r1 = postings[1].rest;
			const hasCostOrPrice = r0.includes('@') || r0.includes('{') || r1.includes('@') || r1.includes('{');

			if (!hasCostOrPrice) {
				const abs0 = Math.abs(a0);
				const abs1 = Math.abs(a1);

				if (abs0 === 0 && abs1 === 0) {
					throw new Error('LLM output invalid: zero amounts in cross-currency postings.');
				}

				// Annotation always goes on the more-valuable-currency posting (smaller absolute amount),
			// expressing how much of the cheaper currency 1 unit of the dearer one buys.
			// Use @ (unit price) when rate has ≤2 decimal places, otherwise @@ (total cost).
				const fxAnnotation = (rateStr: string, total: string, currency: string) => {
					const decimals = rateStr.includes('.') ? rateStr.split('.')[1].length : 0;
					return decimals <= 2 ? ` @ ${rateStr} ${currency}` : ` @@ ${total} ${currency}`;
				};

				if (abs0 <= abs1 && abs0 !== 0) {
					const rate = abs1 / abs0;
					const rateStr = rate.toFixed(8).replace(/\.?0+$/, '');
					postings[0].rest = (postings[0].rest + fxAnnotation(rateStr, postings[1].amount.replace(/^-/, ''), c1)).trim();
				} else if (abs1 !== 0) {
					const rate = abs0 / abs1;
					const rateStr = rate.toFixed(8).replace(/\.?0+$/, '');
					postings[1].rest = (postings[1].rest + fxAnnotation(rateStr, postings[0].amount.replace(/^-/, ''), c0)).trim();
				} else {
					throw new Error('LLM output invalid: one cross-currency posting has zero amount; cannot infer FX rate.');
				}
			}
		}
	}

	const accountWidth = Math.max(...postings.map((p) => p.account.length)) + 2;
	const amountWidth = Math.max(...postings.map((p) => p.amount.length)) + 2;
	const currencyWidth = Math.max(...postings.map((p) => p.currency.length)) + 2;

	const out = [header, ...metadataLines];
	for (const p of postings) {
		let line = `  ${p.account.padEnd(accountWidth)} ${p.amount.padStart(amountWidth)} ${p.currency.padEnd(currencyWidth)}`;
		if (p.rest) line += ` ${p.rest}`;
		out.push(line.trimEnd());
	}

	return out.join('\n');
}

async function callLLMText(
	env: Env,
	userInput: string,
	accounts: string[],
	currencies: Record<string, string>,
	txnDate: string,
	previousDraft?: string,
	declineReason?: string,
	comments: Record<string, string> = {},
	currentTime?: string,
): Promise<string> {
	const acctWithCurr = accountsForPrompt(accounts, currencies, comments);
	const messages = [
		{ role: 'system', content: BEANCOUNT_SYSTEM_PROMPT },
		{ role: 'user', content: buildUserPrompt(txnDate, acctWithCurr, userInput, previousDraft, declineReason, currentTime) },
	];
	const rawText = await callLLMRaw(env, messages, 0.2);
	try {
		return normalizeAndValidateLLMEntry(rawText, accounts);
	} catch (e) {
		throw new Error(`${e}\nInvalid LLM output:\n${rawText}`);
	}
}

async function callLLMVisionGeneric(
	env: Env,
	imageBuffer: ArrayBuffer,
	accounts: string[],
	systemPrompt: string,
	userPrompt: string,
	temperature: number,
): Promise<string> {
	const b64 = arrayBufferToBase64(imageBuffer);
	const messages = [
		{ role: 'system', content: systemPrompt },
		{
			role: 'user',
			content: [
				{ type: 'image_url', image_url: { url: `data:image/jpeg;base64,${b64}` } },
				{ type: 'text', text: userPrompt },
			],
		},
	];
	const rawText = await callLLMRaw(env, messages, temperature);
	try {
		return normalizeAndValidateLLMEntry(rawText, accounts);
	} catch (e) {
		throw new Error(`${e}\nInvalid LLM output:\n${rawText}`);
	}
}

function callLLMVision(
	env: Env, imageBuffer: ArrayBuffer, accounts: string[],
	currencies: Record<string, string>, txnDate: string,
	caption: string, comments: Record<string, string> = {}, currentTime?: string,
): Promise<string> {
	const acctWithCurr = accountsForPrompt(accounts, currencies, comments);
	return callLLMVisionGeneric(env, imageBuffer, accounts, INVEST_ORDER_SYSTEM_PROMPT, buildInvestOrderPrompt(txnDate, acctWithCurr, caption, currentTime), 0.1);
}

function callLLMVisionExpense(
	env: Env, imageBuffer: ArrayBuffer, accounts: string[],
	currencies: Record<string, string>, txnDate: string, caption: string,
	comments: Record<string, string> = {}, currentTime?: string,
): Promise<string> {
	const acctWithCurr = accountsForPrompt(accounts, currencies, comments);
	return callLLMVisionGeneric(env, imageBuffer, accounts, EXPENSE_SCREENSHOT_SYSTEM_PROMPT, buildExpenseScreenshotPrompt(txnDate, acctWithCurr, caption, currentTime), 0.2);
}

// --- Account matching ---

export function matchAccount(suffix: string, accounts: string[]): string | null {
	const suffixLower = suffix.toLowerCase();
	return accounts.find((a) => a.toLowerCase().endsWith(suffixLower)) ?? null;
}

// --- Entry post-processing ---

export function extractNonPnlAccounts(entryText: string): string[] {
	const accounts: string[] = [];
	for (const line of entryText.split('\n')) {
		const m = /^\s+(\S+)\s+/.exec(line);
		if (m && !m[1].startsWith('Expenses') && !m[1].startsWith('Income')) {
			accounts.push(m[1]);
		}
	}
	return accounts;
}

export function buildCommitMessage(prefix: string, entryText: string): string {
	let msg = prefix;
	for (const account of extractNonPnlAccounts(entryText)) {
		msg += `${account}\n`;
	}
	return msg;
}

export function ensureDatetimeMetadata(entryText: string, datetimeStr: string): string {
	if (!entryText) return entryText;
	const lines = entryText.split('\n');

	const hasDatetime = lines.some((l) => /^\s*datetime\s*:\s*".*"\s*$/.test(l));
	if (hasDatetime) return entryText;

	let headerIdx = 0;
	for (let i = 0; i < lines.length; i++) {
		const trimmed = lines[i].trim();
		if (/^\d{4}-\d{2}-\d{2}\s+[*!]\s+/.test(trimmed) || /^\d{4}-\d{2}-\d{2}\s+txn\s+/.test(trimmed)) {
			headerIdx = i;
			break;
		}
	}

	return [
		...lines.slice(0, headerIdx + 1),
		`  datetime: "${datetimeStr}"`,
		...lines.slice(headerIdx + 1),
	].join('\n');
}

export function insertPromptMetadata(entryText: string, userInput: string): string {
	const normalized = (userInput || '').trim().split('\n').join(' ').trim();
	if (!normalized) return entryText;

	const lines = entryText.split('\n');
	if (lines.some(line => /^\s*prompt\s*:/.test(line))) return entryText;

	const escaped = normalized.replace(/"/g, '\\"');
	const metadataLine = `  prompt: "${escaped}"`;

	const headerIdx = lines.findIndex(line =>
		/^\d{4}-\d{2}-\d{2}\s+[*!]\s+/.test(line.trim()) ||
		/^\d{4}-\d{2}-\d{2}\s+txn\s+/.test(line.trim())
	);

	if (headerIdx === -1) return entryText;

	return [...lines.slice(0, headerIdx + 1), metadataLine, ...lines.slice(headerIdx + 1)].join('\n');
}

// --- Templates ---

export function renderOpen(date: string, account: string, currency: string, datetime: string): string {
	return `${date} open ${account} ${currency} ; opened at ${datetime}`;
}

export function renderClose(date: string, account: string, datetime: string): string {
	return `${date} close ${account} ; closed at ${datetime}`;
}

export function renderBalance(date: string, account: string, amount: string, currency: string, datetime: string): string {
	return `${date} balance ${account} ${amount} ${currency} ; updated at ${datetime}`;
}

export function renderPad(date: string, account: string, padAccount: string, datetime: string): string {
	return `${date} pad ${account} ${padAccount} ; updated at ${datetime}`;
}

function renderTransaction(
	date: string,
	payee: string,
	narration: string,
	postings: Posting[],
	tag: string | null,
	link: string | null,
	datetime: string,
): string {
	const accountWidth = Math.max(...postings.map((p) => p.account.length)) + 2;
	const amountWidth = Math.max(...postings.map((p) => p.amount.length)) + 2;
	const currencyWidth = Math.max(...postings.map((p) => p.currency.length)) + 2;
	const restWidth = Math.max(...postings.map((p) => p.rest.length));

	let header: string;
	if (payee && narration) {
		header = `${date} * "${payee}" "${narration}"`;
	} else {
		header = `${date} * "${payee}"`;
	}
	if (tag) header += ` #${tag}`;
	if (link) header += ` ^${link}`;

	let result = header + '\n';
	result += `  datetime: "${datetime}"`;

	for (const p of postings) {
		let line =
			'  ' +
			p.account.padEnd(accountWidth) +
			' ' +
			p.amount.padStart(amountWidth) +
			' ' +
			p.currency.padEnd(currencyWidth);
		if (p.rest) line += ' ' + p.rest.padEnd(restWidth);
		if (p.comment) line += ` ; ${p.comment}`;
		result += '\n' + line.trimEnd();
	}

	return result;
}

// --- Pending entry helpers ---

function buildReviewButtons(pendingId: string): object {
	return {
		inline_keyboard: [
			[
				{ text: '✅', callback_data: `approve:${pendingId}` },
				{ text: '🔧', callback_data: `decline_reason:${pendingId}` },
				{ text: '❌', callback_data: `discard:${pendingId}` },
			],
		],
	};
}

async function savePendingEntry(env: Env, pendingId: string, entry: StoredPendingEntry): Promise<void> {
	await env.KV.put(`pending:${pendingId}`, JSON.stringify(entry), { expirationTtl: DRAFT_TTL_SECONDS });
}

async function getPendingEntry(env: Env, pendingId: string): Promise<StoredPendingEntry | null> {
	return env.KV.get(`pending:${pendingId}`, 'json') as Promise<StoredPendingEntry | null>;
}

async function deletePendingEntry(env: Env, pendingId: string): Promise<void> {
	await env.KV.delete(`pending:${pendingId}`);
}

async function sendDraftForReview(
	env: Env,
	chatId: number,
	draftLabel: string,
	entryText: string,
	userInput: string,
	commitMessage: string,
	dateStr: string,
): Promise<void> {
	const pendingId = crypto.randomUUID();
	await savePendingEntry(env, pendingId, {
		chatId,
		entryText,
		commitMessage,
		userInput,
		dateStr,
		createdAt: Date.now(),
	});

	await sendMessage(
		env,
		chatId,
		`${draftLabel}:\n<pre><code>${escapeHtml(entryText)}</code></pre>\nUse ✅ to save, 🔧 to provide feedback, or ❌ to discard.`,
		{ parseMode: 'HTML', replyMarkup: buildReviewButtons(pendingId) },
	);
}

// --- Recheck with decline reason ---

async function runRecheckWithReason(
	env: Env,
	chatId: number,
	pendingId: string,
	accounts: string[],
	currencies: Record<string, string>,
	declineReason: string,
	comments: Record<string, string> = {},
	currentTime?: string,
): Promise<void> {
	const pending = await getPendingEntry(env, pendingId);
	if (!pending) {
		await sendMessage(env, chatId, 'This request is expired or already handled.');
		return;
	}

	try {
		const newEntry = await callLLMText(env, pending.userInput, accounts, currencies, pending.dateStr, pending.entryText, declineReason, comments, currentTime);
		const entryWithComment = insertPromptMetadata(newEntry, pending.userInput);
		const newCommitMessage = buildCommitMessage('Add entry by Telegram Bot\n\n', entryWithComment);

		await deletePendingEntry(env, pendingId);
		await sendDraftForReview(env, chatId, 'LLM rechecked draft', entryWithComment, pending.userInput, newCommitMessage, pending.dateStr);
	} catch (e) {
		await deletePendingEntry(env, pendingId);
		const errMsg = e instanceof Error ? e.message : String(e);
		await sendMessage(env, chatId, `LLM recheck failed: ${errMsg}`);
	}
}

// --- Callback query handler ---

async function handleCallbackQuery(
	query: {
		id: string;
		data?: string;
		message?: { message_id: number; chat: { id: number } };
	},
	env: Env,
): Promise<void> {
	const callbackId = query.id;
	const data = query.data || '';
	const messageId = query.message?.message_id;
	const chatId = query.message?.chat?.id;

	if (chatId === undefined) {
		await answerCallbackQuery(env, callbackId, 'Unknown chat');
		return;
	}

	const colonIdx = data.indexOf(':');
	if (colonIdx === -1) {
		await answerCallbackQuery(env, callbackId, 'Unknown action');
		return;
	}
	const action = data.slice(0, colonIdx);
	const pendingId = data.slice(colonIdx + 1);

	const pending = await getPendingEntry(env, pendingId);
	if (!pending) {
		await answerCallbackQuery(env, callbackId, 'Expired or already handled');
		if (messageId !== undefined) await editMessageReplyMarkup(env, chatId, messageId);
		return;
	}

	if (pending.chatId !== chatId) {
		await answerCallbackQuery(env, callbackId, 'Not allowed');
		return;
	}

	if (messageId !== undefined) await editMessageReplyMarkup(env, chatId, messageId);

	if (action === 'discard') {
		await Promise.all([
			deletePendingEntry(env, pendingId),
			answerCallbackQuery(env, callbackId, 'Discarded'),
			sendMessage(env, chatId, 'Discarded. Entry was not saved.'),
		]);
		return;
	}

	if (action === 'decline_reason') {
		await Promise.all([
			answerCallbackQuery(env, callbackId, 'Please send reason'),
			env.KV.put(`decline_state:${chatId}`, pendingId, { expirationTtl: DRAFT_TTL_SECONDS }),
			sendMessage(env, chatId, 'Please send your decline reason as plain text. I will send it to LLM for recheck.'),
		]);
		return;
	}

	if (action === 'approve') {
		const [tz, f] = await Promise.all([
			getTimezoneForChat(env, chatId),
			githubDownloadFile(env),
		]);
		if (!f) {
			await Promise.all([
				answerCallbackQuery(env, callbackId, 'Failed'),
				sendMessage(env, chatId, 'Failed to download from GitHub.'),
			]);
			return;
		}

		const { datetimeStr } = formatInTimezone(tz);
		const entryText = ensureDatetimeMetadata(pending.entryText, datetimeStr);

		const ok = await githubUploadFile(env, f.content + '\n' + entryText + '\n', f.sha, pending.commitMessage.trim());
		await deletePendingEntry(env, pendingId);

		if (ok) {
			await Promise.all([
				answerCallbackQuery(env, callbackId, 'Approved'),
				sendMessage(env, chatId, `Created entry:\n<pre><code>${escapeHtml(entryText)}</code></pre>`, { parseMode: 'HTML' }),
			]);
		} else {
			await Promise.all([
				answerCallbackQuery(env, callbackId, 'Failed'),
				sendMessage(env, chatId, 'Failed to upload to GitHub.'),
			]);
		}
		return;
	}

	await answerCallbackQuery(env, callbackId, 'Unknown action');
}

// --- Photo message handler ---

async function handlePhotoMessage(
	message: {
		chat: { id: number };
		photo: Array<{ file_id: string; file_size?: number }>;
		caption?: string;
	},
	env: Env,
): Promise<void> {
	const chatId = message.chat.id;
	const reply = (t: string) => sendMessage(env, chatId, t);

	if (env.CHAT_ID && String(chatId) !== env.CHAT_ID) {
		await reply('How dare you?');
		return;
	}

	const [tz, { accounts, currencies, comments }] = await Promise.all([
		getTimezoneForChat(env, chatId),
		parseAccountsWithCurrencies(env),
	]);
	const { dateStr, timeStr, datetimeStr } = formatInTimezone(tz);

	if (accounts.length === 0) {
		await reply('No accounts available. Please check GitHub account parsing first.');
		return;
	}

	const caption = message.caption?.trim() || '';

	// Highest-resolution photo
	const fileId = message.photo.reduce(
		(max, p) => (p.file_size ?? 0) > (max.file_size ?? 0) ? p : max,
		message.photo[0],
	).file_id;

	const imageBuffer = await getTelegramFileBytes(env, fileId);
	if (!imageBuffer) {
		await reply('Failed to download the image.');
		return;
	}

	const investKeywords = new Set(['invest', 'cfd', 'stocksisa', 'isa']);
	const isInvest = caption.toLowerCase().split(/\s+/).some(w => investKeywords.has(w));

	try {
		if (isInvest) {
			const entry = await callLLMVision(env, imageBuffer, accounts, currencies, dateStr, caption, comments, datetimeStr);
			const entryWithComment = caption ? insertPromptMetadata(entry, caption) : entry;
			const cm = buildCommitMessage('Add investment entry by Telegram Bot\n\n', entryWithComment);
			await sendDraftForReview(env, chatId, 'Investment order draft', entryWithComment, caption || '(investment order screenshot)', cm, dateStr);
		} else {
			const entry = await callLLMVisionExpense(env, imageBuffer, accounts, currencies, dateStr, caption, comments, datetimeStr);
			const entryWithComment = caption ? insertPromptMetadata(entry, caption) : entry;
			const cm = buildCommitMessage('Add expense entry by Telegram Bot\n\n', entryWithComment);
			await sendDraftForReview(env, chatId, 'Expense draft', entryWithComment, caption || '(expense screenshot)', cm, dateStr);
		}
	} catch (e) {
		const errMsg = e instanceof Error ? e.message : String(e);
		const label = isInvest ? 'investment order' : 'expense screenshot';
		await reply(`Failed to process ${label}: ${errMsg}`);
	}
}

// --- Text message handler ---

async function handleMessage(
	message: { chat: { id: number }; text: string },
	env: Env,
): Promise<void> {
	const chatId = message.chat.id;
	let text = message.text;

	const reply = (t: string) => sendMessage(env, chatId, t);

	if (env.CHAT_ID && String(chatId) !== env.CHAT_ID) {
		await reply('How dare you?');
		return;
	}

	const declineStateKey = `decline_state:${chatId}`;
	const [{ accounts, currencies, comments }, tz, waitingPendingId] = await Promise.all([
		parseAccountsWithCurrencies(env),
		getTimezoneForChat(env, chatId),
		env.KV.get(declineStateKey),
	]);

	let { dateStr, datetimeStr, timeStr } = formatInTimezone(tz);
	let customDate = false;

	// Custom date prefix
	if (/^\d{4}-\d{2}-\d{2}/.test(text.trim())) {
		const lines = text.trim().split('\n');
		dateStr = lines[0].trim();
		text = lines.slice(1).join('\n').trim();
		customDate = true;
	}

	// Check for pending decline reason (user is providing feedback for a recheck)
	if (waitingPendingId) {
		const reasonText = text.trim();
		if (!reasonText || reasonText.startsWith('/')) {
			await reply('Please send a non-command reason text, or tap discard.');
			return;
		}
		await env.KV.delete(declineStateKey);
		await runRecheckWithReason(env, chatId, waitingPendingId, accounts, currencies, reasonText, comments, timeStr);
		return;
	}

	let commitMessage = 'Add entry by Telegram Bot\n\n';
	let appendix = '';
	let targetFilePath: string | undefined;

	// --- Slash commands ---
	if (text.startsWith('/')) {
		text = text.substring(1);
		const rawCommand = text.split(' ')[0];
		const command = rawCommand.split('@')[0]; // handle /cmd@botname in groups
		const payload = text.substring(rawCommand.length).trim();

		if (command === 'tz') {
			const newTz = payload;
			if (!isValidTimezone(newTz)) {
				await reply(`Unknown timezone: ${payload}`);
				return;
			}

			await env.KV.put(`tz:${chatId}`, newTz);
			const { datetimeStr: nowStr } = formatInTimezone(newTz);
			await reply(`Timezone set to ${newTz}`);
			await reply(`Current time: ${nowStr}`);
			return;
		}

		if (command === 'update') {
			const parts = payload.split(/\s+/);
			if (parts.length !== 4) {
				await reply('Invalid update command format. Use: /update [account] [account for pad] [amount] [currency]');
				return;
			}

			const account = matchAccount(parts[0], accounts);
			if (!account) { await reply(`No matching account found for suffix: ${parts[0]}`); return; }
			if (!account.startsWith('Expenses') && !account.startsWith('Income')) commitMessage += `${account}\n`;

			const padAccount = matchAccount(parts[1], accounts);
			if (!padAccount) { await reply(`No matching account found for suffix: ${parts[1]}`); return; }

			const amount = parts[2];
			const currency = parts[3];
			const tomorrowStr = addDays(dateStr, 1);

			appendix = renderPad(dateStr, account, padAccount, datetimeStr) + '\n\n' + renderBalance(tomorrowStr, account, amount, currency, datetimeStr);
		} else if (command === 'view') {
			const result = await githubTriggerWorkflow(env, 'monthly-report.yml', {});
			if (result.ok) {
				await reply('Sankey report is being generated.');
			} else {
				await reply(`Failed to trigger the report workflow: ${result.error}`);
			}
			return;
		} else {
			await reply(`Unknown command: ${command}`);
			return;
		}
	}

	// --- open ---
	else if (text.toLowerCase().startsWith('open')) {
		const m = /\S+\s+(\S+)\s+(\S+)/.exec(text);
		if (!m) { await reply('Invalid open command format.'); return; }
		const account = m[1];
		const currency = m[2];
		if (!/^[A-Z][a-zA-Z0-9]*(?::[A-Z][a-zA-Z0-9]*)+$/.test(account)) {
			await reply('Invalid account name. Must be colon-separated capitalized segments, e.g. Assets:Bank:Foo');
			return;
		}
		if (!/^[A-Z][A-Z0-9]{0,9}$/.test(currency)) {
			await reply('Invalid currency. Must be 1-10 uppercase alphanumeric characters starting with a letter, e.g. USD, CNY');
			return;
		}
		const prefix = account.split(':')[0].toLowerCase();
		targetFilePath = ACCOUNT_TYPE_MAP[prefix];
		appendix = renderOpen(dateStr, account, currency, datetimeStr);
	}

	// --- close ---
	else if (text.toLowerCase().startsWith('close')) {
		const m = /\S+\s+(\S+)/.exec(text);
		if (!m) { await reply('Invalid close command format. Use: close [account]'); return; }
		const account = matchAccount(m[1], accounts);
		if (!account) { await reply(`Account not found (no open record): ${m[1]}`); return; }
		const prefix = account.split(':')[0].toLowerCase();
		targetFilePath = ACCOUNT_TYPE_MAP[prefix];
		appendix = renderClose(dateStr, account, datetimeStr);
	}

	// --- balance ---
	else if (text.toLowerCase().startsWith('balance')) {
		const m = /\S+\s+(\S+)\s+(\S+)\s+(\S+)/.exec(text);
		if (!m) { await reply('Invalid balance command format.'); return; }
		const account = matchAccount(m[1], accounts);
		if (!account) { await reply(`No matching account found for suffix: ${m[1]}`); return; }
		// balance assertions take effect at open of stated date, so default to tomorrow
		const balanceDateStr = customDate ? dateStr : addDays(dateStr, 1);
		appendix = renderBalance(balanceDateStr, account, m[2], m[3], datetimeStr);
	}

	// --- pad ---
	else if (text.toLowerCase().startsWith('pad')) {
		const m = /\S+\s+(\S+)\s+(\S+)/.exec(text);
		if (!m) { await reply('Invalid pad command format.'); return; }
		const account = matchAccount(m[1], accounts);
		if (!account) { await reply(`No matching account found for suffix: ${m[1]}`); return; }
		const padAccount = matchAccount(m[2], accounts);
		if (!padAccount) { await reply(`No matching account found for suffix: ${m[2]}`); return; }
		appendix = renderPad(dateStr, account, padAccount, datetimeStr);
	}

	// --- Transaction (structured or LLM) ---
	else {
		const lines = text.split('\n');

		// Try structured format (multi-line with payee + narration + postings)
		if (lines.length >= 4) {
			const payee = lines[0].trim();
			const narration = lines[1].trim();

			let tag: string | null = null;
			let link: string | null = null;
			let lineIdx = 2;

			while (lineIdx < lines.length && lines[lineIdx].trim()) {
				if (lines[lineIdx].startsWith('#')) {
					tag = lines[lineIdx].substring(1).trim();
					lineIdx++;
				} else if (lines[lineIdx].startsWith('^')) {
					link = lines[lineIdx].substring(1).trim();
					lineIdx++;
				} else {
					break;
				}
			}

			const postingLines = lines.slice(lineIdx);
			if (postingLines.filter((l) => l.trim()).length >= 2) {
				const postings: Posting[] = [];
				const rPosting = /^(\S+)\s*(-?\d+\.?\d*)\s*(\S+)\s*(.*?)\s*$/;
				let structuredValid = true;

				for (const raw of postingLines) {
					const trimmed = raw.trim();
					if (!trimmed) continue;

					const semiIdx = trimmed.indexOf(';');
					const [postingStr, comment] =
						semiIdx >= 0
							? [trimmed.substring(0, semiIdx), trimmed.substring(semiIdx + 1).trim()]
							: [trimmed, ''];

					const pm = rPosting.exec(postingStr);
					if (!pm) { structuredValid = false; break; }

					const account = matchAccount(pm[1], accounts);
					if (!account) { structuredValid = false; break; }

					if (!account.startsWith('Expenses') && !account.startsWith('Income')) {
						commitMessage += `${account}\n`;
					}

					const currency = pm[3];
					if (!/^[A-Z0-9][A-Z0-9'._-]*$/.test(currency) || !/[A-Z]/.test(currency)) {
						structuredValid = false;
						break;
					}

					postings.push({ account, amount: pm[2], currency, rest: pm[4] || '', comment });
				}

				if (structuredValid && postings.length >= 2) {
					if (postings.length === 2) {
						const a0 = parseFloat(postings[0].amount);
						const a1 = parseFloat(postings[1].amount);
						const c0 = postings[0].currency;
						const c1 = postings[1].currency;

						if (a0 * a1 >= 0) {
							await reply('两条 posting 必须一正一负。');
							return;
						}
						if (c0 === c1 && Math.abs(a0 + a1) > 0.0001) {
							await reply(`同币种 ${c0} 的两条 posting 金额不平衡：${a0} + ${a1} != 0`);
							return;
						}
						if (c0 !== c1) {
							const r0 = postings[0].rest;
							const r1 = postings[1].rest;
							if (!r0.includes('@') && !r0.includes('{') && !r1.includes('@') && !r1.includes('{')) {
								await reply(`不同币种 (${c0}/${c1}) 的交易需要标记成本 {} 或价格 @。`);
								return;
							}
						}
					}
					appendix = renderTransaction(dateStr, payee, narration, postings, tag, link, datetimeStr);
				}
			}
		}

		// LLM path (single-line, or structured parsing failed)
		if (!appendix) {
			try {
				const entry = await callLLMText(env, text, accounts, currencies, dateStr, undefined, undefined, comments, customDate ? undefined : timeStr);
				const entryWithComment = insertPromptMetadata(entry, text);
				const cm = buildCommitMessage('Add entry by Telegram Bot\n\n', entryWithComment);
				await sendDraftForReview(env, chatId, 'LLM draft (checked padding)', entryWithComment, text, cm, dateStr);
				return;
			} catch (e) {
				const errMsg = e instanceof Error ? e.message : String(e);
				await reply(`❌ LLM 处理失败：\n${errMsg}`);
				return;
			}
		}
	}

	// Direct commit (non-LLM paths: open, close, balance, pad, /update, structured transaction)
	const f = await githubDownloadFile(env, targetFilePath);
	if (!f) { await reply('Failed to download from GitHub.'); return; }

	const ok = await githubUploadFile(env, f.content + '\n' + appendix + '\n', f.sha, commitMessage.trim(), targetFilePath);
	if (ok) {
		await sendMessage(env, chatId, `Created entry:\n<pre><code>${escapeHtml(appendix)}</code></pre>`, { parseMode: 'HTML' });
	} else {
		await reply('Failed to upload to GitHub.');
	}
}

// --- Worker entry ---

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		if (request.method !== 'POST') {
			return new Response('OK');
		}

		if (env.WEBHOOK_SECRET) {
			const secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token');
			if (secret !== env.WEBHOOK_SECRET) {
				return new Response('Unauthorized', { status: 403 });
			}
		} else {
			console.warn('WEBHOOK_SECRET is not set. Webhook requests are not authenticated.');
		}

		try {
			const update = (await request.json()) as {
				message?: {
					chat: { id: number };
					text?: string;
					photo?: Array<{ file_id: string; file_size?: number }>;
					caption?: string;
				};
				callback_query?: {
					id: string;
					data?: string;
					message?: { message_id: number; chat: { id: number } };
				};
			};

			if (update.callback_query) {
				await handleCallbackQuery(update.callback_query, env);
			} else if (update.message?.text) {
				await handleMessage({ chat: update.message.chat, text: update.message.text }, env);
			} else if (update.message?.photo) {
				await handlePhotoMessage(
					{
						chat: update.message.chat,
						photo: update.message.photo,
						caption: update.message.caption,
					},
					env,
				);
			}
		} catch (e) {
			console.error('Error handling update:', e);
		}

		return new Response('OK');
	},
};
