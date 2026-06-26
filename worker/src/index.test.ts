import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	accountsForPrompt,
	addDays,
	buildCommitMessage,
	buildUserPrompt,
	ensureDatetimeMetadata,
	escapeHtml,
	extractAllDirectiveBlocks,
	extractLastDirectiveBlock,
	extractNonPnlAccounts,
	getLLMBackends,
	handleCallbackQuery,
	handleMessage,
	matchAccount,
	normalizeAndValidateLLMEntry,
	preferCurrentAccount,
	insertPromptMetadata,
	renderBalance,
	renderClose,
	renderOpen,
	renderPad,
	splitLines,
	stripCodeFence,
} from './index';

// --- escapeHtml ---

describe('escapeHtml', () => {
	it('escapes &, <, >', () => {
		expect(escapeHtml('a & b < c > d')).toBe('a &amp; b &lt; c &gt; d');
	});
	it('leaves plain text unchanged', () => {
		expect(escapeHtml('hello world')).toBe('hello world');
	});
});

// --- addDays ---

describe('addDays', () => {
	it('adds one day', () => {
		expect(addDays('2024-01-15', 1)).toBe('2024-01-16');
	});
	it('rolls over month boundary', () => {
		expect(addDays('2024-01-31', 1)).toBe('2024-02-01');
	});
	it('handles leap year', () => {
		expect(addDays('2024-02-28', 1)).toBe('2024-02-29');
	});
	it('adds zero days', () => {
		expect(addDays('2024-06-15', 0)).toBe('2024-06-15');
	});
	it('subtracts days with negative input', () => {
		expect(addDays('2024-01-01', -1)).toBe('2023-12-31');
	});
});

// --- accountsForPrompt ---

describe('accountsForPrompt', () => {
	it('annotates accounts that have a currency with parentheses', () => {
		const result = accountsForPrompt(['Assets:WeChat:Current', 'Expenses:Food'], {
			'Assets:WeChat:Current': 'CNY',
		});
		expect(result).toContain('Assets:WeChat:Current (CNY)');
		expect(result).toContain('Expenses:Food');
	});

	it('returns account as-is when no currency', () => {
		const result = accountsForPrompt(['Assets:Cash'], {});
		expect(result).toEqual(['Assets:Cash']);
	});

	it('handles empty lists', () => {
		expect(accountsForPrompt([], {})).toEqual([]);
	});

	it('includes comments when provided', () => {
		const result = accountsForPrompt(
			['Assets:WeChat:Current', 'Expenses:Food'],
			{ 'Assets:WeChat:Current': 'CNY' },
			{ 'Assets:WeChat:Current': '微信支付' },
		);
		expect(result).toContain('Assets:WeChat:Current (CNY) ; 微信支付');
		expect(result).toContain('Expenses:Food');
	});

	it('includes comment without currency', () => {
		const result = accountsForPrompt(
			['Expenses:Food'],
			{},
			{ 'Expenses:Food': '餐饮' },
		);
		expect(result).toEqual(['Expenses:Food ; 餐饮']);
	});
});

// --- buildUserPrompt ---

describe('buildUserPrompt', () => {
	it('includes date, accounts and user input', () => {
		const prompt = buildUserPrompt('2024-01-15', ['Assets:Cash', 'Expenses:Food'], 'lunch 20 USD cash');
		expect(prompt).toContain('2024-01-15');
		expect(prompt).toContain('Assets:Cash');
		expect(prompt).toContain('lunch 20 USD cash');
	});

	it('appends previous draft when provided', () => {
		const prompt = buildUserPrompt('2024-01-15', [], 'coffee', '2024-01-15 * "A" "B"');
		expect(prompt).toContain('Previous declined draft');
		expect(prompt).toContain('2024-01-15 * "A" "B"');
	});

	it('appends decline reason when provided', () => {
		const prompt = buildUserPrompt('2024-01-15', [], 'coffee', undefined, 'wrong account');
		expect(prompt).toContain('Decline reason from user');
		expect(prompt).toContain('wrong account');
	});
});

// --- getLLMBackends ---

describe('getLLMBackends', () => {
	it('parses JSON array from LLM_BACKENDS', () => {
		const backends = JSON.stringify([
			{ LLM_API_BASE_URL: 'https://api.example.com/v1', LLM_API_KEY: 'sk-123', LLM_MODEL: 'gpt-4' },
		]);
		const env = { LLM_BACKENDS: backends } as unknown as Parameters<typeof getLLMBackends>[0];
		const result = getLLMBackends(env);
		expect(result).toHaveLength(1);
		expect(result[0].LLM_MODEL).toBe('gpt-4');
	});

	it('returns empty array when nothing configured', () => {
		const env = {} as unknown as Parameters<typeof getLLMBackends>[0];
		expect(getLLMBackends(env)).toEqual([]);
	});

	it('filters out incomplete backends from array', () => {
		const backends = JSON.stringify([
			{ LLM_API_BASE_URL: 'https://a.com', LLM_API_KEY: '', LLM_MODEL: 'x' },
			{ LLM_API_BASE_URL: 'https://b.com', LLM_API_KEY: 'k', LLM_MODEL: 'm' },
		]);
		const env = { LLM_BACKENDS: backends } as unknown as Parameters<typeof getLLMBackends>[0];
		expect(getLLMBackends(env)).toHaveLength(1);
	});
});

// --- preferCurrentAccount ---

describe('preferCurrentAccount', () => {
	const accounts = ['Assets:WeChat:Current', 'Expenses:Food', 'Liabilities:CreditCard:Chase'];

	it('returns exact match', () => {
		expect(preferCurrentAccount('Expenses:Food', accounts)).toBe('Expenses:Food');
	});

	it('promotes to :Current when child exists', () => {
		expect(preferCurrentAccount('Assets:WeChat', accounts)).toBe('Assets:WeChat:Current');
	});

	it('does not promote Liabilities account', () => {
		expect(preferCurrentAccount('Liabilities:CreditCard:Chase', accounts)).toBe(
			'Liabilities:CreditCard:Chase',
		);
	});

	it('does not double-add :Current', () => {
		expect(preferCurrentAccount('Assets:WeChat:Current', accounts)).toBe('Assets:WeChat:Current');
	});

	it('returns account as-is when no match and no :Current variant', () => {
		expect(preferCurrentAccount('Assets:HSBC', accounts)).toBe('Assets:HSBC');
	});
});

// --- stripCodeFence ---

describe('stripCodeFence', () => {
	it('returns plain text unchanged', () => {
		const text = '2024-01-15 * "A" "B"\n  X  1 USD\n  Y  -1 USD';
		expect(stripCodeFence(text)).toBe(text);
	});

	it('strips ``` fence', () => {
		const inner = '2024-01-15 * "A" "B"\n  X  1 USD\n  Y  -1 USD';
		expect(stripCodeFence(`\`\`\`\n${inner}\n\`\`\``)).toBe(inner);
	});

	it('strips language-tagged fence', () => {
		const inner = '2024-01-15 * "A" "B"\n  X  1 USD\n  Y  -1 USD';
		expect(stripCodeFence(`\`\`\`beancount\n${inner}\n\`\`\``)).toBe(inner);
	});
});

// --- matchAccount ---

describe('matchAccount', () => {
	const accounts = ['Assets:WeChat:Current', 'Assets:Alipay:Current', 'Expenses:Food'];

	it('matches by exact suffix', () => {
		expect(matchAccount('Alipay:Current', accounts)).toBe('Assets:Alipay:Current');
	});

	it('is case-insensitive', () => {
		expect(matchAccount('food', accounts)).toBe('Expenses:Food');
	});

	it('returns null when no match', () => {
		expect(matchAccount('NonExistent', accounts)).toBeNull();
	});
});

// --- extractNonPnlAccounts ---

describe('extractNonPnlAccounts', () => {
	it('extracts Assets and Liabilities, excludes Expenses and Income', () => {
		const entry =
			'2024-01-15 * "Shop" "Lunch"\n  Expenses:Food  20 USD\n  Assets:Cash  -20 USD';
		const result = extractNonPnlAccounts(entry);
		expect(result).toContain('Assets:Cash');
		expect(result).not.toContain('Expenses:Food');
	});

	it('excludes Income accounts', () => {
		const entry =
			'2024-01-15 * "Salary" "Pay"\n  Assets:Bank  5000 USD\n  Income:Salary  -5000 USD';
		const result = extractNonPnlAccounts(entry);
		expect(result).toContain('Assets:Bank');
		expect(result).not.toContain('Income:Salary');
	});
});

// --- buildCommitMessage ---

describe('buildCommitMessage', () => {
	it('appends non-PnL accounts to prefix', () => {
		const entry =
			'2024-01-15 * "Shop" "Lunch"\n  Expenses:Food  20 USD\n  Assets:Cash  -20 USD';
		const result = buildCommitMessage('Add entry\n\n', entry);
		expect(result).toContain('Add entry');
		expect(result).toContain('Assets:Cash');
		expect(result).not.toContain('Expenses:Food');
	});
});

// --- ensureDatetimeMetadata ---

describe('ensureDatetimeMetadata', () => {
	it('inserts datetime after the transaction header', () => {
		const entry = '2024-01-15 * "A" "B"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD';
		const result = ensureDatetimeMetadata(entry, '2024-01-15T12:00:00+00:00');
		const lines = result.split('\n');
		expect(lines[0]).toBe('2024-01-15 * "A" "B"');
		expect(lines[1]).toContain('datetime: "2024-01-15T12:00:00+00:00"');
	});

	it('is idempotent when datetime already present', () => {
		const entry =
			'2024-01-15 * "A" "B"\n  datetime: "2024-01-15T12:00:00+00:00"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD';
		const result = ensureDatetimeMetadata(entry, '2024-01-15T12:00:00+00:00');
		expect(result.split('datetime:').length).toBe(2); // only one occurrence
	});

	it('works with a leading comment line before the header', () => {
		const entry =
			'; original input\n2024-01-15 * "A" "B"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD';
		const result = ensureDatetimeMetadata(entry, '2024-01-15T09:00:00+00:00');
		const lines = result.split('\n');
		const headerIdx = lines.findIndex((l) => l.startsWith('2024-01-15 *'));
		expect(lines[headerIdx + 1]).toContain('datetime:');
	});

	it('returns empty string unchanged', () => {
		expect(ensureDatetimeMetadata('', '2024-01-15T00:00:00+00:00')).toBe('');
	});
});

// --- insertPromptMetadata ---

describe('insertPromptMetadata', () => {
	const entry = '2024-01-15 * "A" "B"\n  X  10 USD\n  Y  -10 USD';

	it('inserts prompt metadata after the header line', () => {
		const result = insertPromptMetadata(entry, 'lunch at KFC');
		const lines = result.split('\n');
		expect(lines[0]).toBe('2024-01-15 * "A" "B"');
		expect(lines[1]).toBe('  prompt: "lunch at KFC"');
	});

	it('is idempotent', () => {
		const withMeta = '2024-01-15 * "A" "B"\n  prompt: "lunch at KFC"\n  X  10 USD\n  Y  -10 USD';
		const result = insertPromptMetadata(withMeta, 'lunch at KFC');
		expect(result.split('prompt:').length).toBe(2);
	});

	it('returns entry unchanged on empty input', () => {
		expect(insertPromptMetadata(entry, '')).toBe(entry);
		expect(insertPromptMetadata(entry, '   ')).toBe(entry);
	});

	it('flattens multi-line user input', () => {
		const result = insertPromptMetadata(entry, 'lunch\nat KFC');
		expect(result).toContain('  prompt: "lunch at KFC"');
	});

	it('escapes double quotes in user input', () => {
		const result = insertPromptMetadata(entry, 'say "hi"');
		expect(result).toContain('  prompt: "say \\"hi\\""');
	});

	it('escapes backslashes in user input', () => {
		const result = insertPromptMetadata(entry, 'path\\to\\file');
		expect(result).toContain('  prompt: "path\\\\to\\\\file"');
	});
});

// --- render helpers ---

describe('renderOpen', () => {
	it('renders an open directive', () => {
		const result = renderOpen('2024-01-15', 'Assets:Cash', 'USD', '2024-01-15 10:00:00');
		expect(result).toBe('2024-01-15 open Assets:Cash USD ; opened at 2024-01-15 10:00:00');
	});
});

describe('renderClose', () => {
	it('renders a close directive', () => {
		const result = renderClose('2024-01-15', 'Assets:Cash', '2024-01-15 10:00:00');
		expect(result).toBe('2024-01-15 close Assets:Cash ; closed at 2024-01-15 10:00:00');
	});
});

describe('renderBalance', () => {
	it('renders a balance directive', () => {
		const result = renderBalance('2024-01-16', 'Assets:Cash', '500', 'USD', '2024-01-15 10:00:00');
		expect(result).toBe(
			'2024-01-16 balance Assets:Cash 500 USD ; updated at 2024-01-15 10:00:00',
		);
	});
});

describe('renderPad', () => {
	it('renders a pad directive', () => {
		const result = renderPad('2024-01-15', 'Assets:Cash', 'Equity:Opening-Balances', '2024-01-15 10:00:00');
		expect(result).toBe(
			'2024-01-15 pad Assets:Cash Equity:Opening-Balances ; updated at 2024-01-15 10:00:00',
		);
	});
});

// --- normalizeAndValidateLLMEntry ---

describe('normalizeAndValidateLLMEntry', () => {
	const accounts = ['Assets:WeChat:Current', 'Expenses:Food', 'Assets:Cash', 'Liabilities:CreditCard:Chase'];

	const validEntry = '2024-01-15 * "KFC" "Lunch"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD';

	it('returns valid entry', () => {
		const result = normalizeAndValidateLLMEntry(validEntry, accounts);
		expect(result).toContain('2024-01-15');
		expect(result).toContain('Expenses:Food');
	});

	it('strips code fences', () => {
		const result = normalizeAndValidateLLMEntry(`\`\`\`\n${validEntry}\n\`\`\``, accounts);
		expect(result).not.toContain('```');
	});

	it('throws when fewer than 3 lines', () => {
		expect(() => normalizeAndValidateLLMEntry('header\n  X  1 USD', accounts)).toThrow();
	});

	it('throws when fewer than two postings', () => {
		const entry = '2024-01-15 * "A" "B"\n  metadata: "x"\n  metadata2: "y"';
		expect(() => normalizeAndValidateLLMEntry(entry, accounts)).toThrow();
	});

	it('throws when postings are unbalanced (same currency)', () => {
		const entry = '2024-01-15 * "A" "B"\n  Expenses:Food  15 USD\n  Assets:Cash  -10 USD';
		expect(() => normalizeAndValidateLLMEntry(entry, accounts)).toThrow();
	});

	it('throws when both postings have same sign', () => {
		const entry = '2024-01-15 * "A" "B"\n  Expenses:Food  10 USD\n  Assets:Cash  10 USD';
		expect(() => normalizeAndValidateLLMEntry(entry, accounts)).toThrow();
	});

	it('promotes account to :Current variant', () => {
		const entry = '2024-01-15 * "Shop" "Coffee"\n  Expenses:Food  20 USD\n  Assets:WeChat  -20 USD';
		const result = normalizeAndValidateLLMEntry(entry, accounts);
		expect(result).toContain('Assets:WeChat:Current');
	});

	it('auto-inserts @@ when rate has more than 2 decimal places', () => {
		// 100 CNY / 13 USD → rate ≈ 7.692... (>2 decimals) → @@
		const entry = '2024-01-15 * "Shop" "Coffee"\n  Expenses:Food  100 CNY\n  Assets:Cash  -13 USD';
		const result = normalizeAndValidateLLMEntry(entry, accounts);
		expect(result).toContain('@@ 100 CNY');
	});

	it('auto-inserts @ when rate has 2 or fewer decimal places', () => {
		// 3000 GBP / 26700 CNY → rate = 8.9 (1 decimal) → @
		const entry = '2024-01-15 * "FX" "Exchange"\n  Assets:WeChat:Current  3000 GBP\n  Assets:Cash  -26700 CNY';
		const result = normalizeAndValidateLLMEntry(entry, accounts);
		expect(result).toContain('@ 8.9 CNY');
		expect(result).not.toContain('@@');
	});

	it('accepts valid 3-posting split-bill entry', () => {
		const entry =
			'2024-01-15 * "Restaurant" "Dinner"\n' +
			'  Liabilities:CreditCard:Chase  -90 USD\n' +
			'  Assets:Cash  45 USD\n' +
			'  Expenses:Food  45 USD';
		const result = normalizeAndValidateLLMEntry(entry, accounts);
		expect(result).toContain('Expenses:Food');
	});
});

// ===========================================================================
// Handler-level regression tests (P1, P2, P3, W-EXTRA-1, W-EXTRA-2)
// These exercise handleMessage / handleCallbackQuery with a fake KV and a
// stubbed global fetch so we can assert routing/ordering behavior.
// ===========================================================================

// Minimal in-memory KVNamespace stand-in
class FakeKV {
	store = new Map<string, string>();
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	async get(key: string, type?: 'json' | 'text'): Promise<any> {
		const v = this.store.get(key);
		if (v === undefined) return null;
		return type === 'json' ? JSON.parse(v) : v;
	}
	async put(key: string, value: string): Promise<void> {
		this.store.set(key, value);
	}
	async delete(key: string): Promise<void> {
		this.store.delete(key);
	}
}

interface RecordedCall {
	url: string;
	method: string;
	// eslint-disable-next-line @typescript-eslint/no-explicit-any
	body: any;
}

const VALID_LLM_ENTRY = '2026-06-25 * "店" "咖啡"\n  Expenses:Food  35 CNY\n  Assets:Cash  -35 CNY';

function jsonResponse(obj: unknown, status = 200): Response {
	return new Response(JSON.stringify(obj), { status, headers: { 'Content-Type': 'application/json' } });
}

// UTF-8-safe base64 encode, matching the worker's encodeBase64/decodeBase64 round-trip
// (btoa alone throws on non-Latin1 characters like Chinese).
function utf8Base64(s: string): string {
	const bytes = new TextEncoder().encode(s);
	let binary = '';
	for (const b of bytes) binary += String.fromCodePoint(b);
	return btoa(binary);
}

function utf8FromBase64(b64: string): string {
	const binary = atob(b64.replaceAll(/\s/g, ''));
	const bytes = new Uint8Array(binary.length);
	for (let i = 0; i < binary.length; i++) bytes[i] = binary.codePointAt(i) ?? 0;
	return new TextDecoder().decode(bytes);
}

function setupFetch(opts: { llmContent?: string; githubDownload?: 'ok' | 'fail'; githubContent?: string; githubSha?: string } = {}): {
	calls: RecordedCall[];
} {
	const calls: RecordedCall[] = [];
	const mock = vi.fn(async (url: string, init?: RequestInit): Promise<Response> => {
		const method = (init?.method ?? 'GET').toUpperCase();
		// eslint-disable-next-line @typescript-eslint/no-explicit-any
		let body: any;
		if (typeof init?.body === 'string') {
			try {
				body = JSON.parse(init.body);
			} catch {
				body = init.body;
			}
		}
		calls.push({ url, method, body });

		if (url.includes('/sendMessage')) return jsonResponse({ ok: true, result: { message_id: 1 } });
		if (url.includes('/editMessageReplyMarkup') || url.includes('/answerCallbackQuery')) return jsonResponse({ ok: true });
		if (url.includes('/chat/completions')) return jsonResponse({ choices: [{ message: { content: opts.llmContent ?? '' } }] });
		if (url.includes('api.github.com') && url.includes('/contents/')) {
			if (method === 'PUT') return new Response('', { status: 200 });
			if (opts.githubDownload === 'fail') return new Response('boom', { status: 500 });
			return jsonResponse({ content: utf8Base64(opts.githubContent ?? '; existing ledger\n'), sha: opts.githubSha ?? 'sha123' });
		}
		return jsonResponse({});
	});
	vi.stubGlobal('fetch', mock as unknown as typeof fetch);
	return { calls };
}

function seedAccounts(kv: FakeKV): void {
	kv.store.set(
		'accounts_cache',
		JSON.stringify({
			accounts: ['Expenses:Food', 'Assets:Cash', 'Assets:WeChat:Current', 'Equity:Opening-Balances'],
			currencies: { 'Assets:Cash': 'CNY' },
			comments: {},
			timestamp: Date.now(),
		}),
	);
}

function makeEnv(kv: FakeKV): Record<string, unknown> {
	return {
		TELEGRAM_BOT_TOKEN: 'test-token',
		GITHUB_TOKEN: 'gh-token',
		REPO_OWNER: 'owner',
		REPO_NAME: 'repo',
		BRANCH_NAME: 'main',
		FILE_PATH: 'main.bean',
		TIMEZONE: 'UTC',
		LLM_BACKENDS: JSON.stringify([{ LLM_API_BASE_URL: 'https://llm.example.com/v1', LLM_API_KEY: 'k', LLM_MODEL: 'm' }]),
		KV: kv,
	};
}

const CHAT_ID = 4242;

function sentMessages(calls: RecordedCall[]): string[] {
	return calls.filter((c) => c.url.includes('/sendMessage')).map((c) => String(c.body?.text ?? ''));
}

function llmCalls(calls: RecordedCall[]): RecordedCall[] {
	return calls.filter((c) => c.url.includes('/chat/completions'));
}

function githubPuts(calls: RecordedCall[]): RecordedCall[] {
	return calls.filter((c) => c.url.includes('api.github.com') && c.url.includes('/contents/') && c.method === 'PUT');
}

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

// --- P1: decline reason is read BEFORE date parsing ---

describe('P1: decline reason read before date parsing', () => {
	function seedPending(kv: FakeKV, pendingId: string): void {
		kv.store.set(`decline_state:${CHAT_ID}`, pendingId);
		kv.store.set(
			`pending:${pendingId}`,
			JSON.stringify({
				chatId: CHAT_ID,
				entryText: VALID_LLM_ENTRY,
				commitMessage: 'Add entry by Telegram Bot\n\n',
				userInput: '咖啡 35',
				dateStr: '2026-06-25',
				createdAt: Date.now(),
			}),
		);
	}

	it('passes a date-keyword-leading reason to recheck intact', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		seedPending(kv, 'pid-1');
		const { calls } = setupFetch({ llmContent: VALID_LLM_ENTRY });

		await handleMessage({ chat: { id: CHAT_ID }, text: '昨天买的，不是今天' }, makeEnv(kv) as never);

		const llm = llmCalls(calls);
		expect(llm).toHaveLength(1);
		expect(JSON.stringify(llm[0].body)).toContain('昨天买的，不是今天');
		// Reason was not reduced/emptied into the guard message
		expect(sentMessages(calls).some((t) => t.startsWith('Please send a non-command reason text'))).toBe(false);
		// decline state consumed
		expect(kv.store.has(`decline_state:${CHAT_ID}`)).toBe(false);
	});

	it('passes an ISO-date-leading reason to recheck intact (worker-specific bug)', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		seedPending(kv, 'pid-2');
		const { calls } = setupFetch({ llmContent: VALID_LLM_ENTRY });

		await handleMessage({ chat: { id: CHAT_ID }, text: '2026-04-17 这个金额不对' }, makeEnv(kv) as never);

		const llm = llmCalls(calls);
		expect(llm).toHaveLength(1);
		// Full raw reason (including the leading date token) reaches the LLM, not emptied
		expect(JSON.stringify(llm[0].body)).toContain('2026-04-17 这个金额不对');
		expect(sentMessages(calls).some((t) => t.startsWith('Please send a non-command reason text'))).toBe(false);
	});
});

// --- P2: directive dispatch uses EXACT equality on post-date first word ---

describe('P2: directive dispatch exact equality', () => {
	it('"opened a beer 5 CNY" routes to the LLM path, not the open handler', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ llmContent: VALID_LLM_ENTRY });

		await handleMessage({ chat: { id: CHAT_ID }, text: 'opened a beer 5 CNY' }, makeEnv(kv) as never);

		// Routed to LLM (transaction path), so the LLM was called...
		expect(llmCalls(calls)).toHaveLength(1);
		// ...and the open-handler validation error was NOT emitted
		expect(sentMessages(calls).some((t) => t.includes('Invalid account name'))).toBe(false);
	});

	it('a real "open Assets:Cash:Wallet CNY" still hits the open handler', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubDownload: 'ok' });

		await handleMessage({ chat: { id: CHAT_ID }, text: 'open Assets:Cash:Wallet CNY' }, makeEnv(kv) as never);

		// Open handler does a direct commit, never an LLM call
		expect(llmCalls(calls)).toHaveLength(0);
		expect(githubPuts(calls)).toHaveLength(1);
		expect(sentMessages(calls).some((t) => t.includes('open Assets:Cash:Wallet CNY'))).toBe(true);
	});
});

// --- P3: approve must not lose a validated draft on download failure ---

describe('P3: approve preserves draft when GitHub download fails', () => {
	function seedDraft(kv: FakeKV, pendingId: string): void {
		kv.store.set(
			`pending:${pendingId}`,
			JSON.stringify({
				chatId: CHAT_ID,
				entryText: VALID_LLM_ENTRY,
				commitMessage: 'Add entry by Telegram Bot\n\n',
				userInput: '咖啡 35',
				dateStr: '2026-06-25',
				createdAt: Date.now(),
			}),
		);
	}

	it('keeps the pending entry and buttons intact on download failure', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		seedDraft(kv, 'pid-approve');
		const { calls } = setupFetch({ githubDownload: 'fail' });

		await handleCallbackQuery(
			{ id: 'cb1', data: 'approve:pid-approve', message: { message_id: 99, chat: { id: CHAT_ID } } },
			makeEnv(kv) as never,
		);

		// Draft still present (not claimed)
		expect(kv.store.has('pending:pid-approve')).toBe(true);
		// Buttons NOT stripped
		expect(calls.some((c) => c.url.includes('/editMessageReplyMarkup'))).toBe(false);
		// No upload attempted
		expect(githubPuts(calls)).toHaveLength(0);
		// User told about the failure
		expect(sentMessages(calls)).toContain('Failed to download from GitHub.');
	});

	it('commits and strips buttons when download succeeds (control)', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		seedDraft(kv, 'pid-ok');
		const { calls } = setupFetch({ githubDownload: 'ok' });

		await handleCallbackQuery(
			{ id: 'cb2', data: 'approve:pid-ok', message: { message_id: 100, chat: { id: CHAT_ID } } },
			makeEnv(kv) as never,
		);

		expect(kv.store.has('pending:pid-ok')).toBe(false);
		expect(calls.some((c) => c.url.includes('/editMessageReplyMarkup'))).toBe(true);
		expect(githubPuts(calls)).toHaveLength(1);
		expect(sentMessages(calls).some((t) => t.startsWith('Created entry:'))).toBe(true);
	});
});

// --- W-EXTRA-1: /update validates the amount before committing ---

describe('W-EXTRA-1: /update amount validation', () => {
	it('rejects a non-numeric amount with Python-parity message and no commit', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubDownload: 'ok' });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/update Cash Opening-Balances notanumber CNY' }, makeEnv(kv) as never);

		expect(sentMessages(calls)).toContain('Invalid amount: notanumber. Must be a valid number.');
		expect(githubPuts(calls)).toHaveLength(0);
	});

	it('accepts a valid numeric amount and commits a balance directive', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubDownload: 'ok' });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/update Cash Opening-Balances 500 CNY' }, makeEnv(kv) as never);

		expect(githubPuts(calls)).toHaveLength(1);
		expect(sentMessages(calls).some((t) => t.includes('balance Assets:Cash 500 CNY'))).toBe(true);
	});
});

// --- W-EXTRA-2: custom-date single-line parsing extracts only the date token ---

describe('W-EXTRA-2: custom-date single-line extracts only the date token', () => {
	// Per the spec, the first-line remainder after the date is dropped (matching Python's
	// ISO-date layer), so a SINGLE-LINE date-prefixed note becomes EMPTY text. Verified
	// against main.py: parse_natural_date("2026-04-17 买咖啡") returns remaining='', and
	// empty (non-single-line) input routes to the structured parser, which replies
	// "Invalid transaction format" and does NOT fall through to the LLM (W2 parity fix).
	it('"2026-04-17 买咖啡" (single line) drops the remainder to empty and replies invalid-format, no LLM', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ llmContent: VALID_LLM_ENTRY });

		await handleMessage({ chat: { id: CHAT_ID }, text: '2026-04-17 买咖啡' }, makeEnv(kv) as never);

		// Empty post-strip text must NOT reach the LLM (Python parity)
		expect(llmCalls(calls)).toHaveLength(0);
		expect(sentMessages(calls)).toContain('Invalid transaction format. Please provide payee, narration and two postings.');
	});

	it('a multi-line date-prefixed message keeps the subsequent lines as input', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ llmContent: VALID_LLM_ENTRY });

		await handleMessage({ chat: { id: CHAT_ID }, text: '2026-04-17 买咖啡\nExpenses:Food 35 CNY' }, makeEnv(kv) as never);

		const llm = llmCalls(calls);
		expect(llm).toHaveLength(1);
		const promptBlob = JSON.stringify(llm[0].body);
		expect(promptBlob).toContain('Transaction date is 2026-04-17. Use this exact date');
		// First-line remainder dropped, but subsequent lines preserved as input
		expect(promptBlob).toContain('Expenses:Food 35 CNY');
	});
});

// ===========================================================================
// W1: /undo, /last, /today port (parity with Python handle_undo/last/today)
// ===========================================================================

// A small ledger with leading ';' comment lines and blank separators, to exercise
// comment inclusion and block boundaries exactly as the Python extractors do.
const LEDGER_FIXTURE = [
	'; header comment',
	'2026-06-01 open Assets:Cash CNY',
	'',
	'; coffee note',
	'2026-06-20 * "Store" "Coffee"',
	'  Expenses:Food  35 CNY',
	'  Assets:Cash  -35 CNY',
	'',
	'2026-06-25 * "Cafe" "Latte"',
	'  Expenses:Food  28 CNY',
	'  Assets:Cash  -28 CNY',
].join('\n');

// Hand-derived to match Python extract_all_directive_blocks(LEDGER_FIXTURE).
const EXPECTED_BLOCKS: Array<[string, string]> = [
	['2026-06-01', '; header comment\n2026-06-01 open Assets:Cash CNY'],
	['2026-06-20', '; coffee note\n2026-06-20 * "Store" "Coffee"\n  Expenses:Food  35 CNY\n  Assets:Cash  -35 CNY'],
	['2026-06-25', '2026-06-25 * "Cafe" "Latte"\n  Expenses:Food  28 CNY\n  Assets:Cash  -28 CNY'],
];

const EXPECTED_LAST_DIRECTIVE = '2026-06-25 * "Cafe" "Latte"\n  Expenses:Food  28 CNY\n  Assets:Cash  -28 CNY';
const EXPECTED_NEW_CONTENT =
	'; header comment\n2026-06-01 open Assets:Cash CNY\n\n; coffee note\n2026-06-20 * "Store" "Coffee"\n  Expenses:Food  35 CNY\n  Assets:Cash  -35 CNY\n';

function lastReplyMarkup(calls: RecordedCall[]): { inline_keyboard: Array<Array<{ text: string; callback_data: string }>> } | null {
	const withMarkup = calls.filter((c) => c.url.includes('/sendMessage') && c.body?.reply_markup);
	return withMarkup.length ? withMarkup[withMarkup.length - 1].body.reply_markup : null;
}

describe('W1: directive block extraction parity', () => {
	it('extractAllDirectiveBlocks returns the same blocks Python would, in file order', () => {
		expect(extractAllDirectiveBlocks(LEDGER_FIXTURE)).toEqual(EXPECTED_BLOCKS);
	});

	it('each block includes its leading ";" comment lines', () => {
		const blocks = extractAllDirectiveBlocks(LEDGER_FIXTURE);
		expect(blocks[0][1].startsWith('; header comment\n')).toBe(true);
		expect(blocks[1][1].startsWith('; coffee note\n')).toBe(true);
		// The last block has no leading comment line
		expect(blocks[2][1].startsWith('2026-06-25')).toBe(true);
	});

	it('extractLastDirectiveBlock returns the last block + trimmed file content', () => {
		const result = extractLastDirectiveBlock(LEDGER_FIXTURE);
		expect(result).not.toBeNull();
		expect(result!.directiveText).toBe(EXPECTED_LAST_DIRECTIVE);
		expect(result!.newContent).toBe(EXPECTED_NEW_CONTENT);
	});

	it('extractLastDirectiveBlock returns null for content with no directive', () => {
		expect(extractLastDirectiveBlock('; just a comment\nnot a directive')).toBeNull();
	});

	it('splitLines mirrors Python splitlines (drops a single trailing newline)', () => {
		expect(splitLines('a\nb\nc')).toEqual(['a', 'b', 'c']);
		expect(splitLines('a\nb\n')).toEqual(['a', 'b']);
		expect(splitLines('')).toEqual([]);
		expect(splitLines('a\n\n')).toEqual(['a', '']);
	});
});

describe('W1: /last command', () => {
	it('defaults to 5 and renders all blocks when fewer than 5 exist', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubContent: LEDGER_FIXTURE });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/last' }, makeEnv(kv) as never);

		const msgs = sentMessages(calls);
		expect(msgs.some((t) => t.startsWith('最近 3 条记录：'))).toBe(true);
		const blob = msgs.join('\n');
		expect(blob).toContain('2026-06-01 open Assets:Cash CNY');
		expect(blob).toContain('2026-06-25 * "Cafe" "Latte"');
	});

	it('shows only the last N blocks for /last 1', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubContent: LEDGER_FIXTURE });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/last 1' }, makeEnv(kv) as never);

		const msgs = sentMessages(calls);
		expect(msgs.some((t) => t.startsWith('最近 1 条记录：'))).toBe(true);
		const blob = msgs.join('\n');
		expect(blob).toContain('2026-06-25 * "Cafe" "Latte"');
		expect(blob).not.toContain('2026-06-01 open');
		expect(blob).not.toContain('2026-06-20');
	});

	it('clamps /last 0 up to 1', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubContent: LEDGER_FIXTURE });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/last 0' }, makeEnv(kv) as never);

		expect(sentMessages(calls).some((t) => t.startsWith('最近 1 条记录：'))).toBe(true);
	});

	it('clamps /last 100 down to 50', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		// 60 single-line balance directives → 60 blocks
		const many = Array.from({ length: 60 }, (_, i) => `2026-01-01 balance Assets:Cash ${i} CNY`).join('\n\n');
		const { calls } = setupFetch({ githubContent: many });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/last 100' }, makeEnv(kv) as never);

		expect(sentMessages(calls).some((t) => t.startsWith('最近 50 条记录：'))).toBe(true);
	});

	it('replies the usage string on a non-numeric arg', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubContent: LEDGER_FIXTURE });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/last abc' }, makeEnv(kv) as never);

		expect(sentMessages(calls)).toContain('用法：/last [数量]，默认 5，最大 50');
	});

	it('reports an empty ledger', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubContent: '; only a comment\n' });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/last' }, makeEnv(kv) as never);

		expect(sentMessages(calls)).toContain('main.bean 中没有找到任何记录。');
	});
});

describe('W1: /today command', () => {
	it('shows only blocks whose date == today', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const today = new Date().toISOString().slice(0, 10); // tz=UTC in makeEnv
		const ledger = [
			'2000-01-01 * "Old" "Past"',
			'  Expenses:Food  10 CNY',
			'  Assets:Cash  -10 CNY',
			'',
			`${today} * "New" "Today"`,
			'  Expenses:Food  20 CNY',
			'  Assets:Cash  -20 CNY',
		].join('\n');
		const { calls } = setupFetch({ githubContent: ledger });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/today' }, makeEnv(kv) as never);

		const msgs = sentMessages(calls);
		expect(msgs.some((t) => t.startsWith(`今天（${today}）共 1 条记录：`))).toBe(true);
		const blob = msgs.join('\n');
		expect(blob).toContain('"New" "Today"');
		expect(blob).not.toContain('"Old" "Past"');
	});

	it('reports the empty case', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const today = new Date().toISOString().slice(0, 10);
		const ledger = [
			'2000-01-01 * "Old" "Past"',
			'  Expenses:Food  10 CNY',
			'  Assets:Cash  -10 CNY',
		].join('\n');
		const { calls } = setupFetch({ githubContent: ledger });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/today' }, makeEnv(kv) as never);

		expect(sentMessages(calls)).toContain(`今天（${today}）没有记录。`);
	});
});

describe('W1: /undo command', () => {
	it('previews the last directive with confirm/cancel buttons and a Revert commit message', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubContent: LEDGER_FIXTURE });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/undo' }, makeEnv(kv) as never);

		// Preview message shows the last directive and the undo prompt
		expect(sentMessages(calls).some((t) => t.startsWith('撤回最后一条指令？') && t.includes('"Cafe" "Latte"'))).toBe(true);
		// Buttons present with undo_confirm / undo_cancel actions
		const markup = lastReplyMarkup(calls);
		expect(markup).not.toBeNull();
		const buttons = markup!.inline_keyboard[0];
		expect(buttons[0].text).toBe('✅ 确认撤回');
		expect(buttons[0].callback_data.startsWith('undo_confirm:')).toBe(true);
		expect(buttons[1].text).toBe('❌ 取消');
		expect(buttons[1].callback_data.startsWith('undo_cancel:')).toBe(true);
		// Pending entry stored with kind: 'undo'
		const pendingId = buttons[0].callback_data.slice('undo_confirm:'.length);
		const stored = JSON.parse(kv.store.get(`pending:${pendingId}`)!);
		expect(stored.kind).toBe('undo');
		expect(stored.commitMessage).toBe('Revert: Cafe Latte');
		expect(stored.fileSha).toBe('sha123');
		// No commit happens on preview
		expect(githubPuts(calls)).toHaveLength(0);
	});

	it('undo_confirm commits the trimmed content with the stored sha (one PUT)', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubContent: LEDGER_FIXTURE });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/undo' }, makeEnv(kv) as never);
		const markup = lastReplyMarkup(calls);
		const confirmData = markup!.inline_keyboard[0][0].callback_data;
		const pendingId = confirmData.slice('undo_confirm:'.length);

		await handleCallbackQuery(
			{ id: 'cb-undo', data: confirmData, message: { message_id: 7, chat: { id: CHAT_ID } } },
			makeEnv(kv) as never,
		);

		const puts = githubPuts(calls);
		expect(puts).toHaveLength(1);
		expect(puts[0].body.sha).toBe('sha123');
		expect(utf8FromBase64(puts[0].body.content)).toBe(EXPECTED_NEW_CONTENT);
		expect(puts[0].body.message).toBe('Revert: Cafe Latte');
		// Pending entry consumed; user notified
		expect(kv.store.has(`pending:${pendingId}`)).toBe(false);
		expect(sentMessages(calls).some((t) => t.startsWith('已撤回以下指令：'))).toBe(true);
	});

	it('undo_cancel discards without committing', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubContent: LEDGER_FIXTURE });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/undo' }, makeEnv(kv) as never);
		const markup = lastReplyMarkup(calls);
		const cancelData = markup!.inline_keyboard[0][1].callback_data;
		const pendingId = cancelData.slice('undo_cancel:'.length);

		await handleCallbackQuery(
			{ id: 'cb-cancel', data: cancelData, message: { message_id: 8, chat: { id: CHAT_ID } } },
			makeEnv(kv) as never,
		);

		expect(githubPuts(calls)).toHaveLength(0);
		expect(kv.store.has(`pending:${pendingId}`)).toBe(false);
		expect(sentMessages(calls)).toContain('已取消，未作任何更改。');
	});

	it('reports when there is no directive to undo', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubContent: '; only a comment\n' });

		await handleMessage({ chat: { id: CHAT_ID }, text: '/undo' }, makeEnv(kv) as never);

		expect(sentMessages(calls)).toContain('main.bean 中没有找到任何指令。');
	});
});

// ===========================================================================
// W2: multi-line input routing aligned to Python
// ===========================================================================

describe('W2: multi-line routing parity', () => {
	it('a multi-line txn with one mistyped account replies the no-matching error and does NOT call the LLM', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ llmContent: VALID_LLM_ENTRY });

		await handleMessage(
			{ chat: { id: CHAT_ID }, text: '店\n咖啡\nExpenses:Food 35 CNY\nAssets:Csah -35 CNY' },
			makeEnv(kv) as never,
		);

		expect(sentMessages(calls)).toContain('No matching account found for suffix: Assets:Csah');
		// Multi-line structured failure must NOT fall through to the LLM
		expect(llmCalls(calls)).toHaveLength(0);
		expect(githubPuts(calls)).toHaveLength(0);
	});

	it('a valid multi-line manual txn still commits (no LLM)', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ githubDownload: 'ok' });

		await handleMessage(
			{ chat: { id: CHAT_ID }, text: '店\n咖啡\nExpenses:Food 35 CNY\nAssets:Cash -35 CNY' },
			makeEnv(kv) as never,
		);

		expect(llmCalls(calls)).toHaveLength(0);
		expect(githubPuts(calls)).toHaveLength(1);
		expect(sentMessages(calls).some((t) => t.startsWith('Created entry:'))).toBe(true);
	});

	it('a genuine single-line note still goes to the LLM', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ llmContent: VALID_LLM_ENTRY });

		await handleMessage({ chat: { id: CHAT_ID }, text: '咖啡 35' }, makeEnv(kv) as never);

		expect(llmCalls(calls)).toHaveLength(1);
	});

	it('a multi-line note that is not a valid transaction replies invalid-format, not the LLM', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ llmContent: VALID_LLM_ENTRY });

		// Two lines only → fails the "< 4 lines" structured floor
		await handleMessage({ chat: { id: CHAT_ID }, text: '买了点东西\n然后又买了点' }, makeEnv(kv) as never);

		expect(sentMessages(calls)).toContain('Invalid transaction format. Please provide payee, narration and two postings.');
		expect(llmCalls(calls)).toHaveLength(0);
	});
});
