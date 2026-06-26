import { afterEach, describe, expect, it, vi } from 'vitest';
import {
	accountsForPrompt,
	addDays,
	buildCommitMessage,
	buildUserPrompt,
	ensureDatetimeMetadata,
	escapeHtml,
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

function setupFetch(opts: { llmContent?: string; githubDownload?: 'ok' | 'fail' } = {}): {
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
			return jsonResponse({ content: btoa('; existing ledger\n'), sha: 'sha123' });
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
	// Per the spec, the first-line remainder after the date is dropped (matching
	// Python's ISO-date layer), so text is empty here; the regression guard is that
	// dateStr is the clean 10-char token, NOT the garbage whole-line.
	it('"2026-04-17 买咖啡" yields a clean 10-char ISO dateStr, not a garbage string', async () => {
		const kv = new FakeKV();
		seedAccounts(kv);
		const { calls } = setupFetch({ llmContent: VALID_LLM_ENTRY });

		await handleMessage({ chat: { id: CHAT_ID }, text: '2026-04-17 买咖啡' }, makeEnv(kv) as never);

		const llm = llmCalls(calls);
		expect(llm).toHaveLength(1);
		const promptBlob = JSON.stringify(llm[0].body);
		// Clean 10-char ISO date used as the transaction date
		expect(promptBlob).toContain('Transaction date is 2026-04-17. Use this exact date');
		// The buggy "date + trailing text" whole-line form must NOT appear
		expect(promptBlob).not.toContain('Transaction date is 2026-04-17 买咖啡');
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
