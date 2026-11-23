"""Check transactions appended to an already checked ledger without replaying its history.

The shadow ledger holds every directive before a cutoff date except transactions, pads and balance assertions, one synthetic transaction per account reproducing its inventory (lots included) just before the cutoff, every original directive dated on or after the cutoff, and the appended transactions. It runs through Beancount's own booking, built-in plugins and validations in memory. It can only accept: any error, unsupported feature or surprise returns None and the caller runs the complete check.
"""

from bisect import bisect_left
from collections import defaultdict
from copy import copy
from datetime import date as Date
from pathlib import Path

from beancount import loader
from beancount.core import convert, data
from beancount.core.data import Balance, Open, Pad, Posting, Transaction
from beancount.core.display_context import DisplayContext
from beancount.core.inventory import Inventory
from beancount.ops import validation
from beancount.parser import booking, parser

from .bot_utils import log, timed
from .ledger_validation import _LOAD_GUARD, _gc_paused, _materialized

BASELINE_FILE = "<shadow-baseline>"
BASELINE_ACCOUNT = "ShadowLedgerBaseline"


def _date(entry):
    return entry.date


def _pipeline(parsed, options):
    """The loader's stages after parsing and sorting, then bean-check's extra validations."""
    booked, errors = booking.book(parsed, options)
    final, errors = loader.run_transformations(booked, list(errors), options, None)
    errors = list(errors) + list(validation.validate(final, options, None, None))
    for check in validation.HARDCORE_VALIDATIONS:
        errors.extend(check(final, options))
    return final, errors


class CheckedLedger:
    """One snapshot that passed the complete check, or an accepted extension of one."""

    def __init__(self, texts, root, journal, entries, options):
        self.texts, self.root, self.journal = texts, root, journal
        self.entries, self.options = entries, options
        self._parsed = None  # Directives before booking, in loader order; parsed on first use.
        self._supported = False
        self._rank = self._journal_file = self._pads = self._balances = None
        self._inventories = self._head = None

    def extend(self, texts):
        """Return the checked ledger for `texts`, or None when only the complete check can decide."""
        try:
            with _gc_paused(), timed("incremental bean-check"):
                return self._extend(texts)
        except Exception as exc:
            log(f"Incremental bean-check unavailable ({type(exc).__name__}: {exc}).")
            return None

    def _appended(self, texts):
        if texts.keys() != self.texts.keys():
            return None
        if any(content != self.texts[path] for path, content in texts.items() if path != self.journal):
            return None
        old, new = self.texts[self.journal], texts[self.journal]
        if not new.startswith(old):
            return None
        suffix = new[len(old):]
        if suffix and not (old.endswith("\n") or suffix.startswith("\n")):
            return None  # The addition would continue the journal's last line.
        return suffix

    def _prepare(self):
        if self._parsed is not None:
            return self._supported
        self._parsed = []
        options = self.options
        # User plugins and document scans may depend on history the shadow does not contain.
        if options["plugin"] or options["documents"] or options["plugin_processing_mode"] != "default":
            return False
        with _materialized(self.texts) as directory, _LOAD_GUARD, timed("incremental bean-check parse"):
            parsed, errors, _ = loader._parse_recursive([(str(Path(directory, self.root)), True)], None)
            journal_file = str(Path(directory, self.journal))
        rank = {}
        for entry in parsed:  # Files are parsed one after another; equal sort keys keep that order.
            rank.setdefault(entry.meta["filename"], len(rank))
        if errors or journal_file not in rank:
            return False
        self._rank, self._journal_file = rank, journal_file
        parsed.sort(key=self._order)
        self._parsed = parsed
        self._pads = defaultdict(list)
        for entry in parsed:
            if isinstance(entry, Pad):
                self._pads[entry.account].append(entry.date)
        self._balances = [(entry.date, entry.account) for entry in parsed if isinstance(entry, Balance)]
        self._supported = True
        return True

    def _order(self, entry):
        return (*data.entry_sortkey(entry), self._rank.get(entry.meta["filename"], -1))

    def _cutoff(self, cut):
        """A pad fills the next balance assertion of its account or a child, so never separate the two."""
        while True:
            asserted = {account for when, account in self._balances if when >= cut}
            earlier = [max(d for d in dates if d < cut) for account, dates in self._pads.items()
                       if any(d < cut for d in dates)
                       and any(a == account or a.startswith(account + ":") for a in asserted)]
            if not earlier:
                return cut
            cut = min(earlier)

    def _inventories_before(self, cut):
        """Every account's inventory and last posting date over entries dated before `cut`."""
        start, inventories, last = 0, defaultdict(Inventory), {}
        if self._inventories and self._inventories[0] <= cut:
            start = bisect_left(self.entries, self._inventories[0], key=_date)
            inventories.update((account, copy(held)) for account, held in self._inventories[1].items())
            last = dict(self._inventories[2])
        for entry in self.entries[start:bisect_left(self.entries, cut, key=_date)]:
            if isinstance(entry, Transaction):
                for posting in entry.postings:
                    inventories[posting.account].add_position(posting)
                    last[posting.account] = entry.date
        self._inventories = (cut, inventories, last)
        return inventories, last

    def _baseline(self, cut):
        inventories, last = self._inventories_before(cut)
        equity = f'{self.options["name_equity"]}:{BASELINE_ACCOUNT}'
        meta = data.new_metadata(BASELINE_FILE, 0)
        entries = [Open(meta, Date(1, 1, 1), equity, None, None)]
        for account, inventory in inventories.items():
            postings = []
            for position in inventory:
                postings.append(Posting(account, position.units, position.cost, None, None, None))
                postings.append(Posting(equity, -convert.get_weight(position), None, None, None, None))
            if postings:
                entries.append(Transaction(meta, last[account], "*", None, "baseline",
                                           data.EMPTY_SET, data.EMPTY_SET, postings))
        return entries

    def _head_before(self, start):
        """Directives before the cutoff that the baseline does not replace: opens, closes, prices and the like."""
        known, head = self._head if self._head and self._head[0] <= start else (0, [])
        head = head + [entry for entry in self._parsed[known:start]
                       if not isinstance(entry, (Transaction, Pad, Balance))]
        self._head = (start, head)
        return head

    def _extend(self, texts):
        suffix = self._appended(texts)
        if suffix is None or not self._prepare():
            return None
        lines_before = self.texts[self.journal].count("\n")
        new, errors, options = parser.parse_string("\n" * lines_before + suffix, report_filename=self._journal_file)
        blank = parser.parse_string("", report_filename=self._journal_file)[2]
        # Only plain transactions qualify: no parse error, other directive, option, plugin or include.
        if (errors or any(not isinstance(entry, Transaction) for entry in new)
                or any(options[key] != blank[key] for key in blank if key != "dcontext")):
            return None
        if not new:
            return self._derived(texts, self._parsed, self.entries, self.options)

        cut = self._cutoff(min(entry.date for entry in new))
        start = bisect_left(self._parsed, cut, key=_date)
        tail = sorted(self._parsed[start:] + new, key=self._order)
        shadow = sorted(self._head_before(start) + self._baseline(cut) + tail, key=self._order)
        display = DisplayContext()
        display.update_from(self.options["dcontext"])
        display.update_from(options["dcontext"])
        display.set_commas(self.options["dcontext"].commas)
        options = {**self.options, "dcontext": display}
        final, errors = _pipeline(shadow, options)
        if errors:
            log(f"Incremental bean-check found {len(errors)} errors; the complete check decides.")
            return None
        entries = self.entries[:bisect_left(self.entries, cut, key=_date)] + [e for e in final if e.date >= cut]
        log(f"Incremental bean-check passed: replayed {len(shadow)} of {len(self._parsed) + len(new)} "
            f"directives from {cut}.")
        return self._derived(texts, self._parsed[:start] + tail, entries, options)

    def _derived(self, texts, parsed, entries, options):
        state = CheckedLedger(texts, self.root, self.journal, entries, options)
        state._parsed, state._supported = parsed, True
        state._rank, state._journal_file = self._rank, self._journal_file
        state._pads, state._balances = self._pads, self._balances  # Only transactions were added.
        # Additions are dated on or after the cutoff just used, so everything cached before it still holds.
        state._inventories, state._head = self._inventories, self._head
        return state
