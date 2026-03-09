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
	/** JSON array of LLMBackend objects, e.g. [{"LLM_API_BASE_URL":"...","LLM_API_KEY":"...","LLM_MODEL":"..."}] */
	LLM_BACKENDS?: string;
	/** Legacy single-backend env vars (used as fallback when LLM_BACKENDS is not set) */
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

interface PendingEntry {
	chatId: number;
	entryText: string;
	createdAt: number;
	messageId?: number;
}

// --- GitHub API ---

const ACCOUNTS_CACHE_TTL = 300; // 5 minutes
const ACCOUNTS_CACHE_KEY = 'accounts_cache';

async function parseAccounts(env: Env): Promise<string[]> {
	// Check cache first
	const cached = await env.KV.get(ACCOUNTS_CACHE_KEY, 'json');
	if (cached) {
		const { accounts, timestamp } = cached as { accounts: string[]; timestamp: number };
		if (Date.now() - timestamp < ACCOUNTS_CACHE_TTL * 1000) {
			return accounts;
		}
	}

	// Fetch from GitHub
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
		return [];
	}

	const files = (await response.json()) as Array<{ name: string; url: string }>;
	const accounts: string[] = [];

	for (const file of files) {
		if (!file.name.endsWith('.bean')) continue;

		const fileResponse = await fetch(file.url, {
			headers: {
				Authorization: `token ${env.GITHUB_TOKEN}`,
				Accept: 'application/vnd.github+json',
				'X-GitHub-Api-Version': '2022-11-28',
			},
		});

		if (!fileResponse.ok) continue;

		const fileData = (await fileResponse.json()) as { content: string };
		const content = decodeBase64(fileData.content);

		// Extract open accounts
		const openMatches = content.matchAll(/\d{4}-\d{2}-\d{2} open (.*)/g);
		for (const match of openMatches) {
			const account = match[1].trim().split(' ')[0];
			accounts.push(account);
		}

		// Remove closed accounts
		const closeMatches = content.matchAll(/\d{4}-\d{2}-\d{2} close (.*)/g);
		for (const match of closeMatches) {
			const account = match[1].trim().split(' ')[0];
			const index = accounts.indexOf(account);
			if (index > -1) {
				accounts.splice(index, 1);
			}
		}
	}

	accounts.sort();

	// Cache the result
	await env.KV.put(
		ACCOUNTS_CACHE_KEY,
		JSON.stringify({ accounts, timestamp: Date.now() }),
		{ expirationTtl: ACCOUNTS_CACHE_TTL * 2 }
	);

	return accounts;
}

// --- Base64 (UTF-8 safe) ---

function encodeBase64(str: string): string {
	const bytes = new TextEncoder().encode(str);
	let binary = '';
	for (const byte of bytes) binary += String.fromCharCode(byte);
	return btoa(binary);
}

function decodeBase64(b64: string): string {
	const binary = atob(b64.replace(/\s/g, ''));
	const bytes = new Uint8Array(binary.length);
	for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
	return new TextDecoder().decode(bytes);
}

// --- Date / Timezone ---

function formatInTimezone(tz: string) {
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
	return { dateStr, datetimeStr };
}

function isValidTimezone(tz: string): boolean {
	try {
		Intl.DateTimeFormat(undefined, { timeZone: tz });
		return true;
	} catch {
		return false;
	}
}

// --- Telegram ---

async function sendMessage(env: Env, chatId: number, text: string) {
	await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ chat_id: chatId, text, parse_mode: 'Markdown' }),
	});
}

// --- GitHub ---

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
		headers: githubHeaders(env),
		body: JSON.stringify({
			message: commitMessage,
			content: encodeBase64(content),
			branch: env.BRANCH_NAME,
			...(sha ? { sha } : {}),
		}),
	});
	return r.status === 200 || r.status === 201;
}

// --- LLM Prompts ---

const BEANCOUNT_SYSTEM_PROMPT = `You are a Beancount assistant. Convert user natural language to ONE beancount entry. CRITICAL: Use ONLY accounts from the provided account list. NEVER create new accounts or sub-accounts that are not in the list. If the account list contains 'Expenses:Food' but not 'Expenses:饮料' or 'Expenses:Drinks', you MUST use 'Expenses:Food'. Treat common payment method names as account hints and map them to the best matching account from the list: 'cash' / 现金 → cash account (e.g. Assets:Cash); 'wechat' / 微信 / 微信支付 → WeChat Pay account; 'alipay' / 支付宝 → Alipay account; '信用卡' / 'credit card' / '刷信用卡' → credit card account (Liabilities:CreditCard:* or Liabilities:Card:*); '银行卡' / 'debit card' / '刷卡' (without '信用') → bank current/debit account (Assets:Bank:*:Current). CRITICAL: When user explicitly mentions '信用卡' or 'credit card', always use a Liabilities account, NOT an Assets account. If the account list contains partial matches (e.g. 'Alipay', 'WeChat', 'WechatPay'), prefer the closest match. When no payment method is mentioned, default to the WeChat Pay or Alipay current/balance account (e.g. :Current or :Balance), NOT investment sub-accounts such as 余額宝 or any account containing 'Fund', 'Investment', or '理财'. If user input does not clearly provide at least one account name/suffix, do NOT create a transaction; instead output exactly one plain text line starting with 'NEED_ACCOUNT:' and explain what account is missing and ask user to edit the input. For the transaction header, payee must be the merchant/service target, not the payment channel. Example: for '微信充值原神', use payee '原神' (not '微信充值原神'). For subscription services, payee should be the service/platform name, and narration should include the subscription tier or specific details. Example: 'chatgpt pro 订阅' → payee 'ChatGPT', narration 'Pro 订阅' (not payee 'ChatGPT Pro', narration '订阅'). When mixing Chinese and English characters in narrations or names, always add a space between Chinese and English text for proper formatting. Examples: 'Pro 订阅' (not 'Pro订阅'), 'Netflix 会员' (not 'Netflix会员'), 'Uber 打车' (not 'Uber打车'). Write the transaction narration (the second quoted string on the header line) in Chinese, unless the user's input is in English. Keep narrations concise and to the point (1-3 words preferred). Avoid verbose descriptions. The narration should describe WHAT was consumed/purchased, not the action verb. CRITICAL for narration: Prefer SPECIFIC items over generic categories. If user mentions a specific item name (e.g., 'coke', 'coffee', 'pizza'), use that item name in the narration, NOT a generic category. Examples: 'coke' → narration 'Coke' (NOT '购物' or '饮料'); 'coffee' → narration 'Coffee' (NOT '饮料'); 'pizza' → narration 'Pizza' (NOT '餐饮'). Use specific meal types from user input: 'brunch' → 'Brunch', 'lunch' → 'Lunch', 'dinner' / 晚餐 → '晚餐', 'breakfast' → 'Breakfast'. Use generic categories ONLY when no specific item is mentioned: '购物', '餐饮', '交通'. DO NOT use action verbs like '吃', '买', '购买' as narration. Examples of good narrations: 'Coke', 'Coffee', 'Brunch', '晚餐', '打车', '转账', '充值'. Capitalise the first letter of each word in person names (e.g. 'john wick' → 'John Wick'). CRITICAL: For internal transfers BETWEEN ASSETS ACCOUNTS (e.g., '转账', 'transfer', moving money from one bank to another), generate EXACTLY TWO postings: one negative from the source Assets account and one positive to the destination Assets account. DO NOT add any Expenses or Income accounts for pure asset transfers. Asset transfers are zero-sum: one account decreases, another increases by the same amount. For transfers between the user's OWN accounts (e.g., 'chase转给globalmoney', 'alipay转到wechat'), use the single-string header format without payee: 'YYYY-MM-DD * "转账"' or 'YYYY-MM-DD * "Transfer"'. For transfers TO ANOTHER PERSON (e.g., '转账给张三', 'transfer to John'), include the recipient's name as payee: 'YYYY-MM-DD * "Zhang San" "转账"' or 'YYYY-MM-DD * "John" "Transfer"'. In most cases, each transaction should have exactly two postings: one negative and one positive. NEVER generate an internal transfer within the same payment platform as part of a simple expense (e.g. do NOT add Assets:WeChat:Current as both debit and credit). For a simple payment via WeChat/Alipay, use exactly one debit posting on the payment account and one credit posting on the Expenses account. When you pay the full amount for a split bill and others transfer their shares back to you, record it in ONE balanced transaction: the full payment as a negative on the paying account, the transfers received back as positive(s) on the receiving account, and the Expenses posting = total paid minus total received back (your net share only). The transaction MUST sum to zero — compute Expenses as the residual. If the user says each person transfers N, use one posting of N per person (not a consolidated sum). When a person's name is associated with a specific posting (e.g. they transferred that amount), add their name as a inline comment on that posting line using ';'. Example: you pay 84 GBP for 4 people; A, B, C each transfer 21 GBP back — postings are: Assets:Bank -84 GBP, Assets:Bank 21 GBP ; A, Assets:Bank 21 GBP ; B, Assets:Bank 21 GBP ; C, Expenses:Food 21 GBP (= 84 - 3×21). If only a total transfer amount is given, one consolidated posting is fine. WRONG: Expenses:Food 84 GBP with Assets:Bank 63 GBP does NOT balance and is incorrect. Never use Income or Assets:Receivable for money transferred back from a split expense. If only one currency appears in the user's input, treat it as the default currency for all amounts in the transaction. Use ISO currency code CNY (not RMB) for Chinese Yuan. Prefer matching Expenses/Income/Assets/Liabilities accounts based on intent. When both parent account and ':Current' child are plausible for payment/deduction, always use the ':Current' account if it exists. For currency conversion, detect the implied FX rate from amounts and include cost/price using '@' or '@@'. Output beancount text only, no markdown, no explanations.

Use this posting style (replace MerchantName and Description with actual values):
YYYY-MM-DD * "MerchantName" "Description"
  Account:Name  -10 USD
  Account:Other  10 USD
`;

function buildUserPrompt(txnDate: string, accounts: string[], userInput: string): string {
	return `Transaction date is ${txnDate}. Use this exact date in the output.
Account list:
${accounts.join('\n')}

User input: ${userInput}
Generate a valid, balanced beancount transaction.`;
}

// --- LLM Functions ---

function preferCurrentAccount(account: string, accounts: string[]): string {
	const accountsLower = new Map(accounts.map((a) => [a.toLowerCase(), a]));

	const lower = account.toLowerCase();
	if (accountsLower.has(lower)) {
		return accountsLower.get(lower)!;
	}

	// Don't add :Current suffix for Liabilities accounts (credit cards)
	if (!lower.startsWith('liabilities:') && !lower.includes(':current')) {
		const currentCandidate = `${account}:Current`;
		const currentLower = currentCandidate.toLowerCase();
		if (accountsLower.has(currentLower)) {
			return accountsLower.get(currentLower)!;
		}
	}

	return account;
}

function stripCodeFence(text: string): string {
	const stripped = text.trim();
	if (stripped.startsWith('```') && stripped.endsWith('```')) {
		const lines = stripped.split('\n');
		if (lines.length >= 3) {
			return lines.slice(1, -1).join('\n').trim();
		}
	}
	return stripped;
}

function normalizeAndValidateLLMEntry(entryText: string, accounts: string[]): string {
	const text = stripCodeFence(entryText);
	const rawLines = text
		.split('\n')
		.map((l) => l.trimEnd())
		.filter((l) => l.trim());

	if (rawLines.length < 3) {
		throw new Error('LLM output is too short. Expected a transaction header and at least two postings.');
	}

	const header = rawLines[0].trim();
	const metadataLines: string[] = [];
	const postings: Array<{ account: string; amount: string; currency: string; rest: string }> = [];

	const postingRe = /^\s*(\S+)\s+(-?\d+(?:\.\d+)?)\s+(\S+)(?:\s+(.*))?$/;

	for (const line of rawLines.slice(1)) {
		const pm = line.match(postingRe);
		if (pm) {
			const account = preferCurrentAccount(pm[1], accounts);
			const amount = pm[2];
			const currency = pm[3];
			const rest = (pm[4] || '').trim();
			postings.push({ account, amount, currency, rest });
		} else {
			metadataLines.push(`  ${line.trim()}`);
		}
	}

	if (postings.length < 2) {
		throw new Error('LLM output must contain at least two postings.');
	}

	const currencies = new Set(postings.map((p) => p.currency));
	if (currencies.size === 1) {
		const total = postings.reduce((sum, p) => sum + parseFloat(p.amount), 0);
		if (Math.abs(total) > 0.0001) {
			throw new Error(`LLM output invalid: postings do not balance (sum = ${total.toFixed(4)}).`);
		}
	}

	if (postings.length === 2) {
		const a0 = parseFloat(postings[0].amount);
		const a1 = parseFloat(postings[1].amount);
		const c0 = postings[0].currency;
		const c1 = postings[1].currency;

		if (a0 * a1 >= 0) {
			throw new Error('LLM output invalid: two postings must be one positive and one negative.');
		}

		if (c0 === c1 && a0 + a1 !== 0) {
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
					const rateStr = rate.toFixed(8).replace(/\.?0+$/, '');
					postings[0].rest = (postings[0].rest + ` @ ${rateStr} ${c1}`).trim();
				} else if (abs1 !== 0) {
					const rate = abs0 / abs1;
					const rateStr = rate.toFixed(8).replace(/\.?0+$/, '');
					postings[1].rest = (postings[1].rest + ` @ ${rateStr} ${c0}`).trim();
				} else {
					const totalStr = abs1.toFixed(8).replace(/\.?0+$/, '');
					postings[0].rest = (postings[0].rest + ` @@ ${totalStr} ${c1}`).trim();
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
		if (p.rest) {
			line += ` ${p.rest}`;
		}
		out.push(line.trimEnd());
	}

	return out.join('\n');
}

function getLLMBackends(env: Env): LLMBackend[] {
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

async function callLLMBackend(backend: LLMBackend, userInput: string, accounts: string[], txnDate: string): Promise<string> {
	const url = `${backend.LLM_API_BASE_URL.replace(/\/$/, '')}/chat/completions`;
	const payload = {
		model: backend.LLM_MODEL,
		temperature: 0.2,
		messages: [
			{ role: 'system', content: BEANCOUNT_SYSTEM_PROMPT },
			{ role: 'user', content: buildUserPrompt(txnDate, accounts, userInput) },
		],
	};

	const response = await fetch(url, {
		method: 'POST',
		headers: {
			Authorization: `Bearer ${backend.LLM_API_KEY}`,
			'Content-Type': 'application/json',
		},
		body: JSON.stringify(payload),
	});

	if (!response.ok) {
		throw new Error(`LLM API error: ${response.status} ${response.statusText}`);
	}

	const data = (await response.json()) as { choices: Array<{ message: { content: string } }> };
	const rawText = data.choices[0].message.content.trim();

	if (rawText.toUpperCase().startsWith('NEED_ACCOUNT:')) {
		const guidance = rawText.includes(':') ? rawText.split(':', 2)[1].trim() : '';
		// NEED_ACCOUNT is a user-input error, not a backend error — don't retry other backends
		throw Object.assign(new Error(guidance || '请在输入中提供至少一个账户名（或账户后缀）'), { isUserError: true });
	}

	try {
		return normalizeAndValidateLLMEntry(rawText, accounts);
	} catch (e) {
		throw new Error(`${e}\nInvalid LLM output:\n${rawText}`);
	}
}

async function callLLM(env: Env, userInput: string, accounts: string[], txnDate: string): Promise<string> {
	const backends = getLLMBackends(env);
	if (backends.length === 0) {
		throw new Error('LLM is not configured. Please set LLM_BACKENDS or LLM_API_BASE_URL/LLM_API_KEY/LLM_MODEL.');
	}

	let lastError: Error | null = null;
	for (const backend of backends) {
		try {
			return await callLLMBackend(backend, userInput, accounts, txnDate);
		} catch (e) {
			const err = e instanceof Error ? e : new Error(String(e));
			if ((err as { isUserError?: boolean }).isUserError) {
				throw err;
			}
			console.error(`LLM backend '${backend.LLM_MODEL}' failed: ${err.message}, trying next...`);
			lastError = err;
		}
	}
	throw new Error(`All LLM backends failed. Last error: ${lastError?.message}`);
}

// --- Accounts ---

function matchAccount(suffix: string, accounts: string[]): string | null {
	const suffixLower = suffix.toLowerCase();
	const matches = accounts.filter((a) => a.toLowerCase().endsWith(suffixLower));
	return matches[0] ?? null;
}

// --- Templates ---

function renderOpen(date: string, account: string, currency: string, datetime: string): string {
	return `${date} open ${account} ${currency} ; opened at ${datetime}`;
}

function renderBalance(date: string, account: string, amount: string, currency: string, datetime: string): string {
	return `${date} balance ${account} ${amount} ${currency} ; updated at ${datetime}`;
}

function renderPad(date: string, account: string, padAccount: string, datetime: string): string {
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
	const restWidth = Math.max(...postings.map((p) => p.rest.length)) + 2;

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
		const line =
			'  ' +
			p.account.padEnd(accountWidth) +
			' ' +
			p.amount.padStart(amountWidth) +
			' ' +
			p.currency.padEnd(currencyWidth) +
			' ' +
			p.rest.padEnd(restWidth) +
			(p.comment ? ` ; ${p.comment}` : '');
		result += '\n' + line.trimEnd();
	}

	return result;
}

// --- Message handling ---

async function handleMessage(update: { message: { chat: { id: number }; text: string } }, env: Env) {
	const chatId = update.message.chat.id;
	let text = update.message.text;

	const reply = (t: string) => sendMessage(env, chatId, t);

	if (env.CHAT_ID && String(chatId) !== env.CHAT_ID) {
		await reply('How dare you?');
		return;
	}

	// Fetch accounts from GitHub
	const accounts = await parseAccounts(env);

	// Timezone from KV or default
	const tzKey = `tz:${chatId}`;
	const tz = (await env.KV.get(tzKey)) || env.TIMEZONE;
	let { dateStr, datetimeStr } = formatInTimezone(tz);

	// Custom date prefix
	if (/^\d{4}-\d{2}-\d{2}/.test(text.trim())) {
		const lines = text.trim().split('\n');
		dateStr = lines[0].trim();
		text = lines.slice(1).join('\n').trim();
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

			await env.KV.put(tzKey, newTz);
			const { datetimeStr: nowStr } = formatInTimezone(newTz);
			await reply(`Timezone set to ${newTz}`);
			await reply(`Current time: ${nowStr}`);
		} else {
			await reply(`Unknown command: ${command}`);
		}
		return;
	}

	// --- Open ---
	if (text.toLowerCase().startsWith('open')) {
		const m = text.match(/\S+\s+(\S+)\s+(\S+)/);
		if (!m) {
			await reply('Invalid open command format.');
			return;
		}
		const accountTypeMap: Record<string, string> = {
			assets: 'accounts/assets.bean',
			liabilities: 'accounts/liabilities.bean',
			equity: 'accounts/equity.bean',
			income: 'accounts/income.bean',
			expenses: 'accounts/expenses.bean',
		};
		const prefix = m[1].split(':')[0].toLowerCase();
		targetFilePath = accountTypeMap[prefix];
		appendix = renderOpen(dateStr, m[1], m[2], datetimeStr);
	}
	// --- Balance ---
	else if (text.toLowerCase().startsWith('balance')) {
		const m = text.match(/\S+\s+(\S+)\s+(\S+)\s+(\S+)/);
		if (!m) {
			await reply('Invalid balance command format.');
			return;
		}
		const account = matchAccount(m[1], accounts);
		if (!account) {
			await reply(`No matching account found for suffix: ${m[1]}`);
			return;
		}
		appendix = renderBalance(dateStr, account, m[2], m[3], datetimeStr);
	}
	// --- Pad ---
	else if (text.toLowerCase().startsWith('pad')) {
		const m = text.match(/\S+\s+(\S+)\s+(\S+)/);
		if (!m) {
			await reply('Invalid pad command format.');
			return;
		}
		const account = matchAccount(m[1], accounts);
		if (!account) {
			await reply(`No matching account found for suffix: ${m[1]}`);
			return;
		}
		const padAccount = matchAccount(m[2], accounts);
		if (!padAccount) {
			await reply(`No matching account found for suffix: ${m[2]}`);
			return;
		}
		appendix = renderPad(dateStr, account, padAccount, datetimeStr);
	}
	// --- Transaction ---
	else {
		const lines = text.split('\n');
		
		// Try structured format
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
			if (postingLines.filter(l => l.trim()).length >= 2) {
				// Try to parse as structured transaction
				const postings: Posting[] = [];
				const rPosting = /^(\S+)\s*(-?\d+\.?\d*)\s*(\S+)\s*(.*?)\s*$/;
				let structuredValid = true;
				
				for (const raw of postingLines) {
					const trimmed = raw.trim();
					if (!trimmed) continue;
					
					const semiIdx = trimmed.indexOf(';');
					const [postingStr, comment] =
						semiIdx >= 0 ? [trimmed.substring(0, semiIdx), trimmed.substring(semiIdx + 1).trim()] : [trimmed, ''];
					
					const pm = postingStr.match(rPosting);
					if (!pm) {
						structuredValid = false;
						break;
					}
					
					const account = matchAccount(pm[1], accounts);
					if (!account) {
						structuredValid = false;
						break;
					}
					
					if (!account.startsWith('Expenses') && !account.startsWith('Income')) {
						commitMessage += `${account}\n`;
					}
					
					const amount = pm[2];
					const currency = pm[3];
					const rest = pm[4] || '';
					
					if (!/^[A-Z0-9][A-Z0-9'._-]*$/.test(currency) || !/[A-Z]/.test(currency)) {
						structuredValid = false;
						break;
					}
					
					postings.push({ account, amount, currency, rest, comment });
				}
				
				if (structuredValid && postings.length >= 2) {
					appendix = renderTransaction(dateStr, payee, narration, postings, tag, link, datetimeStr);
				}
			}
		}
		
		// If not structured or parsing failed, try LLM
		if (!appendix) {
			try {
				appendix = await callLLM(env, text, accounts, dateStr);
				
				// Extract account mentions for commit message
				const accountMatches = appendix.match(/^\s*([A-Z][^\s]+)/gm);
				if (accountMatches) {
					for (const acct of accountMatches) {
						const trimmed = acct.trim();
						if (!trimmed.startsWith('Expenses') && !trimmed.startsWith('Income')) {
							commitMessage += `${trimmed}\n`;
						}
					}
				}
			} catch (e) {
				const errMsg = e instanceof Error ? e.message : String(e);
				await reply(`❌ LLM 处理失败：\n${errMsg}`);
				return;
			}
		}
	}

	// Upload to GitHub
	const f = await githubDownloadFile(env, targetFilePath);
	if (!f) {
		await reply('Failed to download from GitHub.');
		return;
	}
	const ok = await githubUploadFile(env, f.content + '\n' + appendix + '\n', f.sha, commitMessage.trim(), targetFilePath);
	if (ok) {
		await reply('Created entry' + (appendix ? `:\n\`\`\`\n${appendix}\n\`\`\`` : ''));
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
		}

		try {
			const update = (await request.json()) as { message?: { chat: { id: number }; text?: string } };
			if (update.message?.text) {
				await handleMessage(update as { message: { chat: { id: number }; text: string } }, env);
			}
		} catch (e) {
			console.error('Error handling update:', e);
		}

		return new Response('OK');
	},
};
