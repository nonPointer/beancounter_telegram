import { describe, expect, it } from 'vitest';
import {
	accountsForPrompt,
	addDays,
	buildCommitMessage,
	buildUserPrompt,
	ensureDatetimeMetadata,
	escapeHtml,
	extractNonPnlAccounts,
	getLLMBackends,
	matchAccount,
	normalizeAndValidateLLMEntry,
	preferCurrentAccount,
	prependNaturalLanguageComment,
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
	it('annotates accounts that have a currency', () => {
		const result = accountsForPrompt(['Assets:WeChat:Current', 'Expenses:Food'], {
			'Assets:WeChat:Current': 'CNY',
		});
		expect(result).toContain('Assets:WeChat:Current CNY');
		expect(result).toContain('Expenses:Food');
	});

	it('returns account as-is when no currency', () => {
		const result = accountsForPrompt(['Assets:Cash'], {});
		expect(result).toEqual(['Assets:Cash']);
	});

	it('handles empty lists', () => {
		expect(accountsForPrompt([], {})).toEqual([]);
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

	it('falls back to legacy single-backend env vars', () => {
		const env = {
			LLM_API_BASE_URL: 'https://api.example.com/v1',
			LLM_API_KEY: 'sk-abc',
			LLM_MODEL: 'gpt-3.5',
		} as unknown as Parameters<typeof getLLMBackends>[0];
		const result = getLLMBackends(env);
		expect(result).toHaveLength(1);
		expect(result[0].LLM_MODEL).toBe('gpt-3.5');
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
		const result = ensureDatetimeMetadata(entry, '2024-01-15 12:00:00');
		const lines = result.split('\n');
		expect(lines[0]).toBe('2024-01-15 * "A" "B"');
		expect(lines[1]).toContain('datetime: "2024-01-15 12:00:00"');
	});

	it('is idempotent when datetime already present', () => {
		const entry =
			'2024-01-15 * "A" "B"\n  datetime: "2024-01-15 12:00:00"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD';
		const result = ensureDatetimeMetadata(entry, '2024-01-15 12:00:00');
		expect(result.split('datetime:').length).toBe(2); // only one occurrence
	});

	it('works with a leading comment line before the header', () => {
		const entry =
			'; original input\n2024-01-15 * "A" "B"\n  Expenses:Food  10 USD\n  Assets:Cash  -10 USD';
		const result = ensureDatetimeMetadata(entry, '2024-01-15 09:00:00');
		const lines = result.split('\n');
		const headerIdx = lines.findIndex((l) => l.startsWith('2024-01-15 *'));
		expect(lines[headerIdx + 1]).toContain('datetime:');
	});

	it('returns empty string unchanged', () => {
		expect(ensureDatetimeMetadata('', '2024-01-15 00:00:00')).toBe('');
	});
});

// --- prependNaturalLanguageComment ---

describe('prependNaturalLanguageComment', () => {
	const entry = '2024-01-15 * "A" "B"\n  X  10 USD\n  Y  -10 USD';

	it('prepends a comment line', () => {
		const result = prependNaturalLanguageComment(entry, 'lunch at KFC');
		expect(result.startsWith('; lunch at KFC\n')).toBe(true);
	});

	it('is idempotent', () => {
		const withComment = `; lunch at KFC\n${entry}`;
		const result = prependNaturalLanguageComment(withComment, 'lunch at KFC');
		expect(result.split('; lunch at KFC').length).toBe(2);
	});

	it('returns entry unchanged on empty input', () => {
		expect(prependNaturalLanguageComment(entry, '')).toBe(entry);
		expect(prependNaturalLanguageComment(entry, '   ')).toBe(entry);
	});

	it('flattens multi-line user input', () => {
		const result = prependNaturalLanguageComment(entry, 'lunch\nat KFC');
		expect(result.startsWith('; lunch at KFC\n')).toBe(true);
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

	it('auto-inserts FX rate for cross-currency pair', () => {
		const entry = '2024-01-15 * "Shop" "Coffee"\n  Expenses:Food  100 CNY\n  Assets:Cash  -13 USD';
		const result = normalizeAndValidateLLMEntry(entry, accounts);
		expect(result).toContain('@');
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
