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
	/** Legacy single-backend env vars */
	LLM_API_BASE_URL?: string;
	LLM_API_KEY?: string;
	LLM_MODEL?: string;
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
	}).formatToParts(now);

	const get = (type: string) => parts.find((p) => p.type === type)?.value ?? '';
	const dateStr = `${get('year')}-${get('month')}-${get('day')}`;
	const datetimeStr = `${dateStr} ${get('hour')}:${get('minute')}:${get('second')}`;
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
	'You are a Beancount assistant. ' +
	'Convert user natural language to ONE beancount entry. ' +
	'CRITICAL: Use ONLY accounts from the provided account list. NEVER create new accounts or sub-accounts that are not in the list. ' +
	"If the account list contains 'Expenses:Food' but not 'Expenses:饮料' or 'Expenses:Drinks', you MUST use 'Expenses:Food'. " +
	"Treat common payment method names as account hints and map them to the best matching account from the list: " +
	"'cash' / 现金 → cash account (e.g. Assets:Cash); " +
	"'wechat' / 微信 / 微信支付 → WeChat Pay account; " +
	"'alipay' / 支付宝 → Alipay account; " +
	"'信用卡' / 'credit card' / '刷信用卡' → credit card account (Liabilities:CreditCard:* or Liabilities:Card:*); " +
	"'银行卡' / 'debit card' / '刷卡' (without '信用') → bank current/debit account (Assets:Bank:*:Current). " +
	"CRITICAL: When user explicitly mentions '信用卡' or 'credit card', always use a Liabilities account, NOT an Assets account. " +
	"If the account list contains partial matches (e.g. 'Alipay', 'WeChat', 'WechatPay'), prefer the closest match. " +
	"When no payment method is mentioned, default to the WeChat Pay or Alipay current/balance account (e.g. :Current or :Balance), NOT investment sub-accounts such as 余额宝 or any account containing 'Fund', 'Investment', or '理财'. " +
	"If user input does not clearly provide at least one account name/suffix, do NOT create a transaction; " +
	"instead output exactly one plain text line starting with 'NEED_ACCOUNT:' and explain what account is missing and ask user to edit the input. " +
	'For the transaction header, payee must be the merchant/service target, not the payment channel. ' +
	"Example: for '微信充值原神', use payee '原神' (not '微信充值原神'). " +
	'For subscription services, payee should be the service/platform name, and narration should include the subscription tier or specific details. ' +
	"Example: 'chatgpt pro 订阅' → payee 'ChatGPT', narration 'Pro 订阅' (not payee 'ChatGPT Pro', narration '订阅'). " +
	'When mixing Chinese and English characters in narrations or names, always add a space between Chinese and English text for proper formatting. ' +
	"Examples: 'Pro 订阅' (not 'Pro订阅'), 'Netflix 会员' (not 'Netflix会员'), 'Uber 打车' (not 'Uber打车'). " +
	'Write the transaction narration (the second quoted string on the header line) in Chinese, unless the user\'s input is in English. ' +
	'Keep narrations concise and to the point (1-3 words preferred). Avoid verbose descriptions. ' +
	'The narration should describe WHAT was consumed/purchased, not the action verb. ' +
	'CRITICAL for narration: Prefer SPECIFIC items over generic categories. ' +
	"If user mentions a specific item name (e.g., 'coke', 'coffee', 'pizza'), use that item name in the narration, NOT a generic category. " +
	"Examples: 'coke' → narration 'Coke' (NOT '购物' or '饮料'); 'coffee' → narration 'Coffee' (NOT '饮料'); 'pizza' → narration 'Pizza' (NOT '餐饮'). " +
	"Use specific meal types from user input: 'brunch' → 'Brunch', 'lunch' → 'Lunch', 'dinner' / 晚餐 → '晚餐', 'breakfast' → 'Breakfast'. " +
	'When the current time is provided, use it to infer meal context if the user mentions eating without specifying the meal type ' +
	"(e.g., morning → 'Breakfast', noon → 'Lunch', evening → 'Dinner'). " +
	"Use generic categories ONLY when no specific item is mentioned: '购物', '餐饮', '交通'. " +
	"DO NOT use action verbs like '吃', '买', '购买' as narration. " +
	"Examples of good narrations: 'Coke', 'Coffee', 'Brunch', '晚餐', '打车', '转账', '充值'. " +
	"Capitalise the first letter of each word in English person names (e.g. 'john wick' → 'John Wick'). " +
	'CRITICAL: Preserve person names in their ORIGINAL language/script as given in user input. Do NOT romanize, translate, or transliterate names. ' +
	"例如: '张三' must stay '张三', NOT 'Zhang San'; 'たかし' must stay 'たかし', NOT 'Takashi'. " +
	"CRITICAL: For internal transfers BETWEEN ASSETS ACCOUNTS (e.g., '转账', 'transfer', moving money from one bank to another), generate EXACTLY TWO postings: one negative from the source Assets account and one positive to the destination Assets account. " +
	'DO NOT add any Expenses or Income accounts for pure asset transfers. Asset transfers are zero-sum: one account decreases, another increases by the same amount. ' +
	"For transfers between the user's OWN accounts (e.g., 'chase转给globalmoney', 'alipay转到wechat'), use the single-string header format without payee: 'YYYY-MM-DD * \"转账\"' or 'YYYY-MM-DD * \"Transfer\"'. " +
	"For transfers TO ANOTHER PERSON (e.g., '转账给张三', 'transfer to John'), include the recipient's name as payee: 'YYYY-MM-DD * \"张三\" \"转账\"' or 'YYYY-MM-DD * \"John\" \"Transfer\"'. " +
	'In most cases, each transaction should have exactly two postings: one negative and one positive. ' +
	'NEVER generate an internal transfer within the same payment platform as part of a simple expense (e.g. do NOT add Assets:WeChat:Current as both debit and credit). ' +
	'For a simple payment via WeChat/Alipay, use exactly one debit posting on the payment account and one credit posting on the Expenses account. ' +
	'When you pay the full amount for a split bill and others transfer their shares back to you, record it in ONE balanced transaction: ' +
	'the full payment as a negative on the paying account, the transfers received back as positive(s) on the receiving account, and the Expenses posting = total paid minus total received back (your net share only). ' +
	'The transaction MUST sum to zero — compute Expenses as the residual. ' +
	'If the user says each person transfers N, use one posting of N per person (not a consolidated sum). ' +
	"When a person's name is associated with a specific posting (e.g. they transferred that amount), add their name as a inline comment on that posting line using ';'. " +
	'Example: you pay 84 GBP for 4 people; A, B, C each transfer 21 GBP back — postings are: Assets:Bank -84 GBP, Assets:Bank 21 GBP ; A, Assets:Bank 21 GBP ; B, Assets:Bank 21 GBP ; C, Expenses:Food 21 GBP (= 84 - 3×21). ' +
	'If only a total transfer amount is given, one consolidated posting is fine. ' +
	'WRONG: Expenses:Food 84 GBP with Assets:Bank 63 GBP does NOT balance and is incorrect. ' +
	'Never use Income or Assets:Receivable for money transferred back from a split expense. ' +
	'The account list may include annotations after the account name: ' +
	"a default currency in parentheses (e.g. 'Assets:Bank:WeChat (CNY)'), " +
	"and/or a human-readable alias after ';' (e.g. 'Assets:Bank:CMB (CNY) ; 招商银行'). " +
	'CRITICAL: These annotations are NOT part of the account name. ' +
	"When generating beancount postings, use ONLY the account name — never include '(CNY)' or '; alias' in the posting. " +
	"Correct: '  Assets:Bank:CMB  -50 CNY'. Wrong: '  Assets:Bank:CMB (CNY) ; 招商银行  -50 CNY'. " +
	"Use the '; alias' as a hint to match user-mentioned names to the correct account (e.g. user says '招商银行' → use 'Assets:Bank:CMB'). " +
	"When the user does not specify a currency, use the default currency of the payment/source account as shown in the account list. " +
	"If no currency is specified by the user and the payment account has no default currency listed, fall back to context or ask. " +
	"If only one currency appears in the user's input, treat it as the default currency for all amounts in the transaction. " +
	'Use ISO currency code CNY (not RMB) for Chinese Yuan. ' +
	'Prefer matching Expenses/Income/Assets/Liabilities accounts based on intent. ' +
	"When both parent account and ':Current' child are plausible for payment/deduction, always use the ':Current' account if it exists. " +
	"For currency conversion, detect the implied FX rate from amounts and include cost/price using '@' or '@@'. " +
	'Output beancount text only, no markdown, no explanations.\n\n' +
	'Use this posting style (replace MerchantName and Description with actual values):\n' +
	'YYYY-MM-DD * "MerchantName" "Description"\n' +
	'  Account:Name  -10 USD\n' +
	'  Account:Other  10 USD\n';

const INVEST_ORDER_SYSTEM_PROMPT =
	'You are a Beancount assistant specializing in investment order screenshots. ' +
	'Analyze the screenshot and generate ONE valid beancount transaction. ' +
	'CRITICAL: Use ONLY accounts from the provided account list. ' +
	'For BUY orders: ' +
	'  - Debit the stock/ETF holding account using ONLY the @@ total cost (NOT per-unit cost notation): ' +
	'    QUANTITY TICKER @@ TOTAL_COST_WITHOUT_FEE PAYMENT_CURRENCY  ; @ PRICE_PER_SHARE PRICE_CURRENCY ' +
	"    The per-unit price goes in an inline comment (after ';'), NOT in curly-brace cost notation. " +
	'  - If there is an explicit FX fee or trading fee shown in the screenshot, add a separate Expenses posting. ' +
	'  - Credit (negative) the cash/settlement account for the total amount paid (including fees). ' +
	'For SELL orders: ' +
	'  - Debit the cash/settlement account for the net proceeds (the actual cash amount credited, from the screenshot). ' +
	'  - Add a capital gain/loss posting to an Income account for the result/P&L shown in the screenshot: ' +
	'    Income:Investments:CapitalGains  -RESULT_AMOUNT RESULT_CURRENCY ' +
	'    (negative value for a gain, positive value for a loss) ' +
	'  - Credit (negative) the holding account using bare @@ with NO amount after it — beancount computes cost automatically: ' +
	'    -QUANTITY TICKER @@ ' +
	'    Do NOT put any amount or currency after @@. Do NOT calculate or look up cost basis. ' +
	'  - If there is a fee, add a separate Expenses posting. ' +
	'Transaction date: use the fill/execution date from the screenshot, NOT the submission date. ' +
	"Payee: broker or platform name (e.g. 'Trading 212', 'IBKR', 'Robinhood'). " +
	"Narration: format as 'Buy QUANTITY TICKER (Full Company Name)' or 'Sell QUANTITY TICKER (Full Company Name)', e.g. 'Buy 15.5 GOOGL (Google)' or 'Sell 10 AAPL (Apple)'. " +
	'If no fee is shown in the screenshot, do NOT invent a fee posting. ' +
	'Output beancount text only, no markdown, no explanations.\n\n' +
	'Example BUY with FX conversion:\n' +
	'2026-03-06 * "Trading 212" "Buy 15.5 GOOGL (Google)"\n' +
	'  Assets:Broker:GOOGL      15.5 GOOGL @@ 3464.78 GBP  ; @ 297.75 USD\n' +
	'  Expenses:Investments:Fee   5.20 GBP\n' +
	'  Assets:Broker:Cash       -3469.98 GBP\n' +
	'\n' +
	'Example SELL with capital gain (result = 266.98 GBP from screenshot, net proceeds = 2406.54 GBP):\n' +
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

function buildInvestOrderPrompt(txnDate: string, accountsWithCurrencies: string[], currentTime?: string): string {
	const timeInfo = currentTime ? ` (current time: ${currentTime})` : '';
	return (
		`Reference date (today): ${txnDate}${timeInfo}.\n` +
		'Account list:\n' +
		accountsWithCurrencies.join('\n') +
		'\n\nAnalyze the investment order screenshot and generate the beancount transaction. ' +
		'Use the fill/execution date shown in the screenshot as the transaction date.'
	);
}

// --- LLM functions ---

export function getLLMBackends(env: Env): LLMBackend[] {
	if (env.LLM_BACKENDS) {
		try {
			const parsed = JSON.parse(env.LLM_BACKENDS) as LLMBackend[];
			return parsed.filter((b) => b.LLM_API_BASE_URL && b.LLM_API_KEY && b.LLM_MODEL);
		} catch {
			// fall through to legacy
		}
	}
	if (env.LLM_API_BASE_URL && env.LLM_API_KEY && env.LLM_MODEL) {
		return [{ LLM_API_BASE_URL: env.LLM_API_BASE_URL, LLM_API_KEY: env.LLM_API_KEY, LLM_MODEL: env.LLM_MODEL }];
	}
	return [];
}

async function callLLMRaw(env: Env, messages: unknown[], temperature: number): Promise<string> {
	const backends = getLLMBackends(env);
	if (backends.length === 0) {
		throw new Error('LLM is not configured. Please set LLM_BACKENDS or LLM_API_BASE_URL/LLM_API_KEY/LLM_MODEL.');
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

			const data = (await response.json()) as { choices: Array<{ message: { content: string } }> };
			const rawText = data.choices[0].message.content.trim();

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

				if (abs0 <= abs1 && abs0 !== 0) {
					const rate = abs1 / abs0;
					postings[0].rest = (postings[0].rest + ` @ ${rate.toFixed(8).replace(/\.?0+$/, '')} ${c1}`).trim();
				} else if (abs1 !== 0) {
					const rate = abs0 / abs1;
					postings[1].rest = (postings[1].rest + ` @ ${rate.toFixed(8).replace(/\.?0+$/, '')} ${c0}`).trim();
				} else {
					postings[0].rest = (postings[0].rest + ` @@ ${abs1.toFixed(8).replace(/\.?0+$/, '')} ${c1}`).trim();
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

async function callLLMVision(
	env: Env,
	imageBuffer: ArrayBuffer,
	accounts: string[],
	currencies: Record<string, string>,
	txnDate: string,
	comments: Record<string, string> = {},
	currentTime?: string,
): Promise<string> {
	const acctWithCurr = accountsForPrompt(accounts, currencies, comments);
	const b64 = arrayBufferToBase64(imageBuffer);
	const messages = [
		{ role: 'system', content: INVEST_ORDER_SYSTEM_PROMPT },
		{
			role: 'user',
			content: [
				{ type: 'image_url', image_url: { url: `data:image/jpeg;base64,${b64}` } },
				{ type: 'text', text: buildInvestOrderPrompt(txnDate, acctWithCurr, currentTime) },
			],
		},
	];
	const rawText = await callLLMRaw(env, messages, 0.1);
	try {
		return normalizeAndValidateLLMEntry(rawText, accounts);
	} catch (e) {
		throw new Error(`${e}\nInvalid LLM output:\n${rawText}`);
	}
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
		if (/^\d{4}-\d{2}-\d{2}\s+\*\s+/.test(lines[i].trim())) {
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

export function prependNaturalLanguageComment(entryText: string, userInput: string): string {
	const normalized = (userInput || '')
		.trim()
		.split('\n')
		.join(' ')
		.trim();
	if (!normalized) return entryText;

	const commentLine = `; ${normalized}`;
	const firstLine = entryText.trim().split('\n')[0].trim();
	if (firstLine === commentLine) return entryText;

	return `${commentLine}\n${entryText}`;
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
		const entryWithComment = prependNaturalLanguageComment(newEntry, pending.userInput);
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
	const { dateStr, timeStr } = formatInTimezone(tz);

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

	try {
		const entry = await callLLMVision(env, imageBuffer, accounts, currencies, dateStr, comments, timeStr);
		const entryWithComment = caption ? prependNaturalLanguageComment(entry, caption) : entry;
		const cm = buildCommitMessage('Add investment entry by Telegram Bot\n\n', entryWithComment);
		await sendDraftForReview(env, chatId, 'Investment order draft', entryWithComment, caption || '(investment order screenshot)', cm, dateStr);
	} catch (e) {
		const errMsg = e instanceof Error ? e.message : String(e);
		await reply(`Failed to process investment order: ${errMsg}`);
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
			let newTz = payload;
			if (payload === 'London') newTz = 'Europe/London';
			else if (payload === 'Beijing') newTz = 'Asia/Shanghai';

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
		const prefix = m[1].split(':')[0].toLowerCase();
		targetFilePath = ACCOUNT_TYPE_MAP[prefix];
		appendix = renderOpen(dateStr, m[1], m[2], datetimeStr);
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
					appendix = renderTransaction(dateStr, payee, narration, postings, tag, link, datetimeStr);
				}
			}
		}

		// LLM path (single-line, or structured parsing failed)
		if (!appendix) {
			try {
				const entry = await callLLMText(env, text, accounts, currencies, dateStr, undefined, undefined, comments, customDate ? undefined : timeStr);
				const entryWithComment = prependNaturalLanguageComment(entry, text);
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
