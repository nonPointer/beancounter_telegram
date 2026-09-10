"""Post-save BQL summaries shared by manual and reviewed transactions."""

from collections import Counter
from datetime import datetime, timedelta

from beancount.core.data import Transaction
from beancount.core.inventory import Inventory
from beancount.parser import parser

from .bot_utils import _capped_code_block, format_query_result, log, timed


def _amounts(inventory):
    return ", ".join(str(position.units) for position in sorted(inventory, key=lambda p: p.units.currency)) or "0"


def _account_labels(accounts):
    """Drop type/bank prefixes, restoring segments when short names collide."""
    parts = {account: account.split(":") for account in accounts}
    starts = {account: 2 if len(names) > 2 and names[1] == "Bank" else 1 for account, names in parts.items()}
    while True:
        labels = {account: ":".join(names[starts[account]:]) for account, names in parts.items()}
        counts = Counter(labels.values())
        collisions = [account for account, label in labels.items() if counts[label] > 1 and starts[account] > 0]
        if not collisions:
            return labels
        for account in collisions:
            starts[account] -= 1


class ReportMixin:
    def transaction_report_tables(self, appendix, *, today=None, loaded=None):
        """Use one checked snapshot; balances are exact accounts in original units."""
        directives, errors, _ = parser.parse_string(appendix)
        transactions = [entry for entry in directives if isinstance(entry, Transaction)]
        if errors:
            raise ValueError("Cannot parse the saved transaction for reporting")
        if not transactions:
            return []
        accounts = sorted({posting.account for entry in transactions for posting in entry.postings
                           if posting.account.startswith(("Assets:", "Liabilities:"))})
        today = today or datetime.now(self.timezone).date()
        first_day = today.replace(day=1)
        next_month = (first_day + timedelta(days=32)).replace(day=1)
        loaded = loaded if loaded is not None else self.load_ledger()
        if loaded is None:
            raise ValueError("Complete ledger unavailable for reporting")
        with timed("post-save BQL reports"):
            _, expenses = self.run_bql(
                f"SELECT root(account, 2) AS category, cost(sum(position)) AS total "
                f"WHERE account ~ '^Expenses:' AND date >= {first_day} AND date < {next_month} "
                "GROUP BY category ORDER BY category", loaded=loaded)
            total = Inventory()
            expense_rows = []
            for category, amount in expenses:
                total.add_inventory(amount)
                expense_rows.append((category.removeprefix("Expenses:"), _amounts(amount)))
            if expense_rows:
                expense_rows.append(("合计", _amounts(total)))
            expense_text = format_query_result([("类别", str), ("金额", str)], expense_rows) if expense_rows else "本月暂无开支。"
            tables = [(f"{first_day:%Y-%m} 分类开支（按成本计，多币种分别列示）", expense_text)]
            if accounts:
                # Account names come from parsed postings, and equality excludes subaccounts.
                predicate = " OR ".join(f"account = '{account}'" for account in accounts)
                _, balances = self.run_bql(
                    f"SELECT account, units(sum(position)) AS balance WHERE {predicate} "
                    "GROUP BY account ORDER BY account", loaded=loaded)
                by_account = dict(balances)
                labels = _account_labels(accounts)
                rows = [(labels[account], _amounts(by_account.get(account, Inventory()))) for account in accounts]
                tables.append(("本笔交易涉及的账户余额（账本全部日期，原币种／持仓单位）",
                               format_query_result([("账户", str), ("余额", str)], rows)))
            else:
                tables.append(("本笔交易涉及的账户余额", "本笔交易未涉及 Assets／Liabilities 账户。"))
            return tables

    def send_transaction_report(self, chat_id, appendix):
        """Report failures cannot change the outcome of an already successful save."""
        try:
            tables = self.transaction_report_tables(appendix)
            for title, table in tables:
                block, truncated = _capped_code_block(table, 3500)
                notice = "\n（统计内容过长，已截断显示。）" if truncated else ""
                self.send_message(chat_id, f"{title}\n{block}{notice}", parse_mode="HTML")
        except Exception as exc:
            log(f"Post-save report failed ({type(exc).__name__}: {exc}); transaction remains saved.")
            try:
                self.send_message(chat_id, "交易已保存，但统计暂时无法生成。无需重复记账。")
            except Exception as notify_error:
                log(f"Post-save report notification failed ({type(notify_error).__name__}: {notify_error}).")
