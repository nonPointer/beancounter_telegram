"""Incremental (shadow ledger) checks must agree with the complete bean-check."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import datetime
import random
import unittest
from collections import Counter
from unittest.mock import MagicMock, patch

from beancount.parser import printer

from beancounter import drafts, ledger_shadow
from beancounter.ledger_shadow import CheckedLedger
from beancounter.ledger_validation import load_ledger_texts
from test_bot import MOCK_CONFIG, make_bot

ROOT, JOURNAL = "main.bean", "journal/2026.bean"
START = datetime.date(2026, 1, 1)
DAYS = 90
CASH = ["Assets:Cash", "Assets:Bank:A", "Assets:Bank:B", "Liabilities:Card"]
EXP = ["Expenses:Food", "Expenses:Misc"]
UNSTABLE_OPTIONS = {"include", "filename", "input_hash", "dcontext"}


def setUpModule():
    for target in ("beancounter.ledger_validation.log", "beancounter.ledger_shadow.log", "beancounter.bot_utils.log"):
        patcher = patch(target)
        patcher.start()
        unittest.addModuleCleanup(patcher.stop)


def ledger(journal, accounts="", extra="", options=""):
    opens = "".join(f"2025-12-01 open {name}\n" for name in
                    CASH + EXP + ["Income:Salary", "Income:PnL", "Equity:Opening", "Assets:Broker", "Assets:Old"])
    return {ROOT: f'{options}include "accounts/all.bean"\ninclude "{JOURNAL}"\ninclude "extra.bean"\n',
            "accounts/all.bean": accounts or opens, JOURNAL: journal, "extra.bean": extra}


def full(texts):
    return load_ledger_texts(texts, ROOT, JOURNAL)


def checked(texts):
    entries, errors, options = full(texts)
    assert not errors, errors
    return CheckedLedger(texts, ROOT, JOURNAL, entries, options)


def appended(texts, addition):
    return {**texts, JOURNAL: texts[JOURNAL] + "\n" + addition + "\n"}


def rendered(entries):
    return [printer.format_entry(entry) for entry in entries]


class EquivalenceMixin:
    def assert_same_as_full(self, state, texts):
        entries, errors, options = full(texts)
        self.assertEqual(errors, [])
        self.assertEqual(rendered(state.entries), rendered(entries))
        self.assertEqual([e.meta["lineno"] for e in state.entries], [e.meta["lineno"] for e in entries])
        self.assertEqual({k: v for k, v in state.options.items() if k not in UNSTABLE_OPTIONS},
                         {k: v for k, v in options.items() if k not in UNSTABLE_OPTIONS})
        self.assertEqual(str(state.options["dcontext"]), str(options["dcontext"]))


SEED = ('2025-12-02 * "seed"\n  Assets:Bank:A 50000 CNY\n  Assets:Cash 3000 USD\n'
        "  Assets:Old 10 CNY\n  Equity:Opening\n")


class TestShadowRules(EquivalenceMixin, unittest.TestCase):
    def test_plain_transaction_matches_complete_check(self):
        base = ledger(SEED)
        texts = appended(base, '; telegram-operation: x\n2026-01-05 * "Shop" "tea"\n  prompt: "tea 12.345"\n'
                               "  Expenses:Food 12.345 CNY\n  Assets:Cash -1.5 EUR @@ 12.345 CNY")
        state = checked(base).extend(texts)
        self.assert_same_as_full(state, texts)

    def test_later_balance_assertion_still_guards_the_addition(self):
        base = ledger(SEED + "2026-01-06 balance Assets:Bank:A 50000 CNY\n")
        texts = appended(base, '2026-01-05 * "spend"\n  Expenses:Food 5 CNY\n  Assets:Bank:A')
        self.assertTrue(full(texts)[1])
        self.assertIsNone(checked(base).extend(texts))

    def test_pad_before_the_addition_is_replayed_with_its_balance(self):
        base = ledger(SEED + "2026-01-02 pad Assets:Bank:A Equity:Opening\n2026-01-09 balance Assets:Bank:A 60000 CNY\n")
        texts = appended(base, '2026-01-05 * "spend"\n  Expenses:Food 5 CNY\n  Assets:Bank:A')
        state = checked(base).extend(texts)
        self.assert_same_as_full(state, texts)

    def test_lots_survive_in_the_baseline(self):
        buys = ('2026-01-02 * "buy"\n  Assets:Broker 5 STOCK {10 CNY}\n  Assets:Bank:A\n'
                '2026-01-03 * "buy"\n  Assets:Broker 5 STOCK {12 CNY, "second"}\n  Assets:Bank:A\n')
        base = ledger(SEED + buys)
        sold = appended(base, '2026-02-01 * "sell"\n  Assets:Broker -5 STOCK {"second"} @ 13 CNY\n'
                              "  Assets:Bank:A 65 CNY\n  Income:PnL")
        state = checked(base).extend(sold)
        self.assert_same_as_full(state, sold)
        again = appended(sold, '2026-02-02 * "sell"\n  Assets:Broker -1 STOCK {12 CNY} @ 13 CNY\n'
                               "  Assets:Bank:A 13 CNY\n  Income:PnL")
        self.assertTrue(full(again)[1])  # That lot is gone.
        self.assertIsNone(state.extend(again))

    def test_chained_additions_reuse_the_state(self):
        texts = ledger(SEED)
        state = checked(texts)
        with patch.object(ledger_shadow, "_materialized", wraps=ledger_shadow._materialized) as parse:
            for day in (5, 5, 9, 3, 9):
                texts = appended(texts, f'2026-01-{day:02d} * "spend"\n  Expenses:Food {day} CNY\n  Assets:Cash')
                state = state.extend(texts)
                self.assert_same_as_full(state, texts)
        self.assertEqual(parse.call_count, 1)

    def test_equal_sort_keys_keep_file_order(self):
        addition = '2026-01-05 * "journal"\n  Assets:Broker 1 STOCK {10 CNY}\n  Assets:Bank:A'
        base = ledger(SEED)
        line = appended(base, addition)[JOURNAL].split("\n").index('2026-01-05 * "journal"')
        extra = "\n" * line + '2026-01-05 * "extra"\n  Assets:Broker 1 STOCK {11 CNY}\n  Assets:Bank:A\n'
        base = ledger(SEED, extra=extra)
        texts = appended(base, addition)
        state = checked(base).extend(texts)
        self.assert_same_as_full(state, texts)
        payees = [e.narration for e in state.entries if getattr(e, "narration", None) in ("journal", "extra")]
        self.assertEqual(payees, ["journal", "extra"])

    def test_comment_only_addition(self):
        base = ledger(SEED)
        texts = appended(base, "; nothing to book")
        self.assert_same_as_full(checked(base).extend(texts), texts)

    def test_unchanged_candidate(self):
        base = ledger(SEED)
        self.assert_same_as_full(checked(base).extend(dict(base)), base)

    def test_cases_left_to_the_complete_check(self):
        spend = '2026-01-05 * "spend"\n  Expenses:Food 5 CNY\n  Assets:Cash'
        base = ledger(SEED)
        cases = {
            "balance directive": appended(base, "2026-01-05 balance Assets:Cash 3000 USD"),
            "open directive": appended(base, "2026-01-05 open Assets:New"),
            "option": appended(base, 'option "title" "x"\n' + spend),
            "plugin": appended(base, 'plugin "beancount.plugins.auto_accounts"\n' + spend),
            "include": appended(base, 'include "other.bean"\n' + spend),
            "pushtag": appended(base, "pushtag #trip\n" + spend),
            "syntax error": appended(base, spend + "\n  what"),
            "unbalanced": appended(base, '2026-01-05 * "bad"\n  Expenses:Food 5 CNY\n  Assets:Cash -4 CNY'),
            "unknown account": appended(base, '2026-01-05 * "bad"\n  Expenses:Nope 5 CNY\n  Assets:Cash'),
            "another file changed": {**appended(base, spend), "extra.bean": "; changed\n"},
            "file added": {**appended(base, spend), "new.bean": ""},
            "journal rewritten": {**base, JOURNAL: "; rewritten\n" + base[JOURNAL] + spend + "\n"},
            "journal shortened": {**base, JOURNAL: base[JOURNAL][:-5]},
        }
        state = checked(base)
        for name, texts in cases.items():
            with self.subTest(name):
                self.assertIsNone(state.extend(texts))
        self.assertIsNotNone(state.extend(appended(base, spend)))

    def test_addition_continuing_the_last_line(self):
        base = ledger(SEED.rstrip("\n"))
        state = checked(base)
        self.assertIsNone(state.extend({**base, JOURNAL: base[JOURNAL] + '2026-01-05 * "x"\n  Expenses:Food 5 CNY\n  Assets:Cash\n'}))
        texts = appended(base, '2026-01-05 * "x"\n  Expenses:Food 5 CNY\n  Assets:Cash')
        self.assert_same_as_full(state.extend(texts), texts)

    def test_ledgers_left_to_the_complete_check(self):
        spend = '2026-01-05 * "spend"\n  Expenses:Food 5 CNY\n  Assets:Cash'
        for name, base in {
            "user plugin": ledger(SEED, options='plugin "beancount.plugins.auto_accounts"\n'),
            "documents": ledger(SEED, options='option "documents" "docs"\n'),
            "raw mode": ledger(SEED, options='option "plugin_processing_mode" "raw"\n'),
            "journal without directives": {**ledger("; empty\n"), "extra.bean": SEED},
        }.items():
            with self.subTest(name):
                entries, _, options = full(base)  # Document scans report a missing directory; the rule is what matters.
                self.assertIsNone(CheckedLedger(base, ROOT, JOURNAL, entries, options).extend(appended(base, spend)))

    def test_open_tag_or_metadata_scope_never_passes_the_complete_check(self):
        # An unclosed pushtag/pushmeta would also apply to appended text, so such a journal must never become a base.
        for scope in ("pushtag #trip", 'pushmeta trip: "x"'):
            with self.subTest(scope):
                self.assertTrue(full(ledger(SEED + scope + "\n"))[1])

    def test_internal_failure_falls_back(self):
        base = ledger(SEED)
        state = checked(base)
        with patch.object(ledger_shadow.booking, "book", side_effect=RuntimeError("changed internals")):
            self.assertIsNone(state.extend(appended(base, '2026-01-05 * "x"\n  Expenses:Food 5 CNY\n  Assets:Cash')))


def directive(rng, late=False):
    day = START + datetime.timedelta(days=rng.randint(DAYS - 6, DAYS) if late else rng.randint(0, DAYS))
    kind = rng.random()
    amount = rng.choice([1, 5, 12.5, 80, 300, 1234.56])
    currency = rng.choice(["CNY", "CNY", "USD"])
    if kind < 0.40:
        return f'{day} * "Shop" "spend"\n  {rng.choice(EXP)} {amount} {currency}\n  {rng.choice(CASH)}'
    if kind < 0.48:
        return f'{day} * "move"\n  {rng.choice(CASH)} {amount} {currency}\n  {rng.choice(CASH)} -{amount} {currency}'
    if kind < 0.60:
        return (f'{day} * "buy"\n  Assets:Broker {rng.randint(1, 9)} STOCK {{{rng.choice([10, 11, 12.5])} CNY}}\n'
                "  Assets:Bank:A")
    if kind < 0.72:
        units = rng.randint(1, 12)
        return (f'{day} * "sell"\n  Assets:Broker -{units} STOCK {rng.choice(["{}", "{}", "{10 CNY}", "{12.5 CNY}"])}'
                f" @ 13 CNY\n  Assets:Bank:A {units * 13} CNY\n  Income:PnL")
    if kind < 0.78:
        return f'{day} * "fx"\n  Assets:Cash -{amount} USD @ 7.1 CNY\n  Assets:Bank:B'
    if kind < 0.84:
        return f'{day} * "salary"\n  Assets:Bank:A 9000 CNY\n  Income:Salary'
    if kind < 0.88:
        return f"{day} price STOCK {rng.choice([11, 12, 13])} CNY"
    if kind < 0.93:
        account = rng.choice(CASH + ["Assets:Bank", "Assets"])
        return f"{day} balance {account} {rng.choice([0, 100, 5000, 41234.5, 50000, -80])} {currency}"
    if kind < 0.98:
        account = rng.choice(CASH[:3])
        later = day + datetime.timedelta(days=rng.randint(1, 6))
        return f"{day} pad {account} Equity:Opening\n{later} balance {account} {rng.choice([100, 2500, 60000])} {currency}"
    if kind < 0.99:
        return f'{day} * "drain"\n  Assets:Old -10 CNY\n  Expenses:Misc\n{day} close Assets:Old'
    return f"{day} close {rng.choice(['Assets:Bank:B', 'Liabilities:Card'])}"


def candidate(rng):
    day = START + datetime.timedelta(days=rng.randint(DAYS - 6, DAYS))
    kind = rng.random()
    if kind < 0.08:
        return f'{day} * "bad" "unbalanced"\n  Expenses:Food 5 CNY\n  Assets:Cash -4 CNY'
    if kind < 0.14:
        return f'{day} * "bad" "unknown account"\n  Expenses:Nope 5 CNY\n  Assets:Cash'
    if kind < 0.20:
        return f'{day} * "bad" "currency"\n  Expenses:Food 5 EUR\n  {rng.choice(CASH)}'
    if kind < 0.26:
        return f'{day} * "old account"\n  Expenses:Food 5 CNY\n  Assets:Old'
    while True:
        text = directive(rng, late=rng.random() < 0.8)
        if " * " in text.splitlines()[0] and "close" not in text:
            return text


class TestShadowAgainstCompleteCheck(EquivalenceMixin, unittest.TestCase):
    """Random ledgers: every verdict and every accepted result must equal the complete check's."""

    def run_ledger(self, seed, stats):
        rng = random.Random(seed)
        booking = rng.choice([None, None, "FIFO", "LIFO", "NONE"])
        options = f'option "booking_method" "{booking}"\n' if booking else ""
        accounts = "".join(f"2025-12-01 open {a} {rng.choice(['CNY,USD', 'CNY,USD', ''])}".rstrip() + "\n" for a in CASH)
        accounts += "".join(f"2025-12-01 open {a}\n" for a in EXP + ["Income:Salary", "Income:PnL", "Equity:Opening",
                                                                    "Assets:Broker", "Assets:Old"])
        accounts += "2025-12-01 commodity STOCK\n"
        texts = ledger(SEED, accounts=accounts, options=options)
        for _ in range(rng.randint(15, 45)):  # Grow a clean ledger across two files, in file order.
            path = "extra.bean" if rng.random() < 0.2 else JOURNAL
            grown = {**texts, path: texts[path] + "\n" + directive(rng) + "\n"}
            if not full(grown)[1]:
                texts = grown
        state = checked(texts)
        for _ in range(12):
            grown = appended(texts, candidate(rng))
            valid = not full(grown)[1]
            extended = state.extend(grown)
            self.assertEqual(extended is not None, valid, f"seed {seed}: {grown[JOURNAL][len(texts[JOURNAL]):]}")
            stats["accepted" if valid else "rejected"] += 1
            if valid:
                stats["backdated cutoffs"] += state._inventories[0] < min(
                    e.date for e in extended._parsed if e not in state._parsed)
                self.assert_same_as_full(extended, grown)
                texts, state = grown, extended  # Chain on the incremental state to expose drift.

    def test_random_ledgers(self):
        stats = Counter()
        for seed in range(int(os.environ.get("SHADOW_LEDGERS", "30"))):
            self.run_ledger(seed, stats)
        # The generator must keep exercising both verdicts and the pad rule.
        self.assertGreater(stats["accepted"], 100)
        self.assertGreater(stats["rejected"], 40)
        self.assertGreater(stats["backdated cutoffs"], 5)


class TestCommitGate(EquivalenceMixin, unittest.TestCase):
    """The bot's commit gate: incremental when it can, complete otherwise, never weaker."""

    def setUp(self):
        self.bot = make_bot()
        self.addCleanup(self.bot.close)
        self.bot.settings.FILE_PATH = JOURNAL
        self.bot.settings.LEDGER_ROOT = ROOT
        self.base = ledger(SEED)
        complete = patch.object(drafts, "check_ledger", wraps=drafts.check_ledger)
        self.complete = complete.start()
        self.addCleanup(complete.stop)

    def load(self, texts):
        entries, options = drafts.check_ledger(texts, ROOT, JOURNAL)
        self.complete.reset_mock()
        self.bot._ledger_cache = {"tree_sha": "tree", "entries": entries, "options_map": options, "texts": texts}

    def spend(self, texts, amount=5, day=5):
        return appended(texts, f'2026-01-{day:02d} * "spend"\n  Expenses:Food {amount} CNY\n  Assets:Cash')

    def test_first_candidate_without_a_checked_ledger_gets_the_complete_check(self):
        texts = self.spend(self.base)
        self.bot._check_commit_candidate(texts)
        self.complete.assert_called_once()
        self.bot._check_commit_candidate(self.spend(texts, 6))
        self.complete.assert_called_once()  # The checked candidate is itself a base now.

    def test_loaded_ledger_is_the_base(self):
        self.load(self.base)
        texts = self.spend(self.base)
        entries, options = self.bot._check_commit_candidate(texts)
        self.complete.assert_not_called()
        self.assert_same_as_full(CheckedLedger(texts, ROOT, JOURNAL, entries, options), texts)

    def test_drafts_prepared_against_the_same_ledger_and_saved_in_turn(self):
        self.load(self.base)
        first, second = self.spend(self.base, 5), self.spend(self.base, 6)
        self.bot._check_commit_candidate(first)
        self.bot._check_commit_candidate(second)
        checked_first = self.bot._check_commit_candidate(first)
        self.bot._remember_checked_ledger(first, checked_first)
        both = self.spend(first, 6)
        entries, options = self.bot._check_commit_candidate(both)
        self.complete.assert_not_called()
        self.assert_same_as_full(CheckedLedger(both, ROOT, JOURNAL, entries, options), both)

    def test_invalid_candidate_reports_the_complete_check(self):
        self.load(self.base)
        bad = appended(self.base, '2026-01-05 * "bad"\n  Expenses:Food 5 CNY\n  Assets:Cash -4 CNY')
        with self.assertRaisesRegex(ValueError, "bean-check failed"):
            self.bot._check_commit_candidate(bad)
        self.complete.assert_called_once()
        self.bot._check_commit_candidate(self.spend(self.base))
        self.complete.assert_called_once()

    def test_external_change_gets_the_complete_check(self):
        self.load(self.base)
        changed = {**self.base, "extra.bean": '2026-01-02 * "elsewhere"\n  Expenses:Food 1 CNY\n  Assets:Cash\n'}
        self.bot._check_commit_candidate(self.spend(changed))
        self.complete.assert_called_once()

    def test_known_ledgers_are_bounded(self):
        self.load(self.base)
        for amount in range(1, 9):
            self.bot._check_commit_candidate(self.spend(self.base, amount))
        self.assertLessEqual(len(self.bot._checked_ledgers), 4)
        self.complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
