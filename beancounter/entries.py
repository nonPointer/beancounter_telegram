"""Entries responsibilities of Bot; shared entry points are preserved."""

from decimal import Decimal
from beancount.parser import parser as beancount_parser
import re
from .bot_utils import (
    BALANCE_TOLERANCE, _is_txn_header,
)

class EntryMixin:
    def prefer_current_account(self, account: str, accounts: list[str]) -> str:
        accounts_by_lower = {a.lower(): a for a in accounts}

        if account.lower() in accounts_by_lower:
            return accounts_by_lower[account.lower()]

        # Don't add :Current suffix for Liabilities accounts (credit cards)
        if not account.lower().startswith("liabilities:"):
            if ":current" not in account.lower():
                current_candidate = f"{account}:Current"
                if current_candidate.lower() in accounts_by_lower:
                    return accounts_by_lower[current_candidate.lower()]

        return account

    def strip_code_fence(self, text: str) -> str:
        stripped = text.strip()
        # Remove code fence markers (```lang or ```) but only the marker line itself
        # Use [ \t]* instead of \s* to avoid crossing line boundaries
        cleaned = re.sub(r'^[ \t]*```\w*[ \t]*$', '', stripped, flags=re.MULTILINE).strip()
        # Extract beancount entry: find the transaction header and collect from there
        # Use [ \t]* instead of \s* to avoid matching across line boundaries
        header_re = re.compile(r'^[ \t]*\d{4}-\d{2}-\d{2}\s+[*!txn]', re.MULTILINE)
        m = header_re.search(cleaned)
        if m:
            # Collect leading ; comment lines immediately before the header
            before = cleaned[:m.start()]
            comments = []
            for line in reversed(before.splitlines()):
                if line.strip().startswith(';'):
                    comments.insert(0, line)
                elif line.strip() == '':
                    continue
                else:
                    break
            # Collect header + all subsequent indented/posting/comment lines
            after = cleaned[m.start():]
            entry_lines = []
            for i, line in enumerate(after.splitlines()):
                if i == 0:
                    entry_lines.append(line.strip())
                elif line.strip() == '' or line[0] in (' ', '\t') or line.strip().startswith(';'):
                    entry_lines.append(line)
                else:
                    break
            return "\n".join(comments + entry_lines).strip()
        return cleaned

    def normalize_and_validate_llm_entry(self, entry_text: str, accounts: list[str]) -> str:
        text = self.strip_code_fence(entry_text)
        raw_lines = [line.rstrip() for line in text.splitlines() if line.strip()]
        if len(raw_lines) < 3:
            raise ValueError("LLM output is too short. Expected a transaction header and at least two postings.")

        # Skip leading ; comment lines to find the header
        header_idx = 0
        leading_comments = []
        for idx, line in enumerate(raw_lines):
            if line.strip().startswith(';'):
                leading_comments.append(line.strip())
            else:
                header_idx = idx
                break

        header = raw_lines[header_idx].strip()
        # Validate header looks like a beancount directive (YYYY-MM-DD ...)
        if not re.match(r'^\d{4}-\d{2}-\d{2}\s+', header):
            raise ValueError(f"LLM output invalid: first line is not a beancount directive header: {header!r}")

        metadata_lines = [f"  {c}" if not c.startswith('  ') else c for c in leading_comments]
        postings = []

        posting_re = re.compile(r'^\s*(\S+)\s+(-?\d+(?:\.\d+)?)\s+(\S+)(?:\s+(.*))?$')
        # Beancount metadata: key-value (e.g. "  key: value") or inline comments ("; ...")
        # Keys must start with [a-z] per beancount spec.
        metadata_re = re.compile(r'^\s*([a-z][a-zA-Z0-9_-]*\s*:.*|;.*)$')

        # Strip parenthesized currency/alias annotations that LLMs sometimes copy
        # from the account list (e.g. "Assets:Bank:CMB (CNY)" → "Assets:Bank:CMB")
        paren_annotation_re = re.compile(r'\s+\([^)]*\)(?=\s)')

        for line in raw_lines[header_idx + 1:]:
            line = paren_annotation_re.sub('', line)
            pm = posting_re.match(line)
            if pm:
                account = self.prefer_current_account(pm.group(1), accounts)
                amount = pm.group(2)
                currency = pm.group(3)
                rest = (pm.group(4) or "").strip()
                postings.append({
                    "account": account,
                    "amount": amount,
                    "currency": currency,
                    "rest": rest,
                })
                continue

            # Only keep valid beancount metadata/comment lines; skip natural language
            if metadata_re.match(line):
                metadata_lines.append(f"  {line.strip()}")

        if len(postings) < 2:
            raise ValueError("LLM output must contain at least two postings.")

        currencies = set(p["currency"] for p in postings)
        if len(currencies) == 1:
            total = sum(Decimal(p["amount"]) for p in postings)
            if abs(total) > BALANCE_TOLERANCE:
                raise ValueError(f"LLM output invalid: postings do not balance (sum = {total:.4f}).")

        if len(postings) == 2:
            a0 = Decimal(postings[0]["amount"])
            a1 = Decimal(postings[1]["amount"])
            c0 = postings[0]["currency"]
            c1 = postings[1]["currency"]
            r0 = postings[0]["rest"]
            r1 = postings[1]["rest"]

            if a0 * a1 >= 0:
                raise ValueError("LLM output invalid: two postings must be one positive and one negative.")

            if c0 == c1 and abs(a0 + a1) > BALANCE_TOLERANCE:
                raise ValueError(f"LLM output invalid: same-currency postings are unbalanced ({a0} + {a1} != 0).")

            if c0 != c1:
                has_cost_or_price = any(('@' in r or '{' in r) for r in [r0, r1])
                if not has_cost_or_price:
                    # Auto-insert FX price annotation when LLM misses @/@@ on cross-currency postings.
                    abs0, abs1 = abs(a0), abs(a1)
                    if abs0 == 0 and abs1 == 0:
                        raise ValueError("LLM output invalid: zero amounts in cross-currency postings.")

                    # Annotation always goes on the more-valuable-currency posting (smaller absolute
                    # amount), expressing how much of the cheaper currency 1 unit of the dearer one
                    # buys.  Use @ (unit price) when rate has ≤2 decimal places, otherwise @@ (total).
                    def _fx_annotation(rate_str: str, total_str: str, currency: str) -> str:
                        decimals = len(rate_str.split('.')[1]) if '.' in rate_str else 0
                        if decimals <= 2:
                            return f" @ {rate_str} {currency}"
                        return f" @@ {total_str} {currency}"

                    if abs0 <= abs1 and abs0 != 0:
                        rate = abs1 / abs0
                        rate_str = f"{rate:.8f}".rstrip('0').rstrip('.')
                        postings[0]["rest"] = (postings[0]["rest"] + _fx_annotation(rate_str, postings[1]["amount"].lstrip('-'), c1)).strip()
                    elif abs1 != 0:
                        rate = abs0 / abs1
                        rate_str = f"{rate:.8f}".rstrip('0').rstrip('.')
                        postings[1]["rest"] = (postings[1]["rest"] + _fx_annotation(rate_str, postings[0]["amount"].lstrip('-'), c0)).strip()
                    else:
                        # abs0 > 0, abs1 == 0: degenerate cross-currency posting; cannot infer FX rate.
                        raise ValueError(
                            "LLM output invalid: one cross-currency posting has zero amount; cannot infer FX rate."
                        )

        account_width = max(len(p["account"]) for p in postings) + 2
        amount_width = max(len(str(p["amount"])) for p in postings) + 2
        currency_width = max(len(p["currency"]) for p in postings) + 2

        out = [header]
        out.extend(metadata_lines)
        for p in postings:
            line = (
                "  "
                + p["account"].ljust(account_width)
                + " "
                + p["amount"].rjust(amount_width)
                + " "
                + p["currency"].ljust(currency_width)
            )
            if p["rest"]:
                line += f" {p['rest']}"
            out.append(line.rstrip())

        return "\n".join(out)

    def validate_beancount_syntax(self, entry_text: str) -> str | None:
        """Validate entry with the beancount parser. Returns error string or None."""
        entries, errors, _ = beancount_parser.parse_string(entry_text)
        if errors:
            return "; ".join(e.message for e in errors)
        if not entries:
            return "No valid beancount entry parsed"
        return None

    def extract_accounts_from_entry(self, entry_text: str) -> list[str]:
        accounts = []
        posting_line_re = re.compile(r'^\s+(\S+)\s+')
        for line in entry_text.splitlines():
            m = posting_line_re.match(line)
            if m:
                token = m.group(1)
                # Skip metadata lines (key: value) — only real posting lines start with an account name.
                if re.match(r'^[A-Z][A-Za-z0-9]*(?::[A-Z][A-Za-z0-9\-]*)+$', token):
                    accounts.append(token)
        return accounts

    def validate_accounts_exist(self, entry_text: str, accounts: list[str]) -> str | None:
        """Check every posting account in entry_text exists in the known accounts list."""
        known = set(accounts)
        used = self.extract_accounts_from_entry(entry_text)
        missing = [a for a in used if a not in known]
        if missing:
            # De-duplicate preserving order.
            seen = set()
            unique_missing = [a for a in missing if not (a in seen or seen.add(a))]
            return f"Unknown account(s): {', '.join(unique_missing)}"
        return None

    def ensure_datetime_metadata(self, entry_text: str, datetime_str: str) -> str:
        lines = entry_text.splitlines()
        if not lines:
            return entry_text

        has_datetime = any(re.match(r'^\s*datetime\s*:\s*".*"\s*$', line) for line in lines)
        if has_datetime:
            return entry_text

        # Insert datetime after the transaction header, even when leading comment lines exist.
        header_idx = None
        for idx, line in enumerate(lines):
            if _is_txn_header(line):
                header_idx = idx
                break

        if header_idx is None:
            header_idx = 0

        return "\n".join(lines[: header_idx + 1] + [f'  datetime: "{datetime_str}"'] + lines[header_idx + 1 :])

    def insert_prompt_metadata(self, entry_text: str, user_input: str) -> str:
        normalized = " ".join((user_input or "").strip().splitlines()).strip()
        if not normalized:
            return entry_text

        lines = entry_text.splitlines()
        if any(re.match(r'^\s*prompt\s*:', line) for line in lines):
            return entry_text

        escaped = normalized.replace('\\', '\\\\').replace('"', '\\"')
        metadata_line = f'  prompt: "{escaped}"'

        header_idx = None
        for idx, line in enumerate(lines):
            if _is_txn_header(line):
                header_idx = idx
                break

        if header_idx is None:
            return entry_text

        return "\n".join(lines[:header_idx + 1] + [metadata_line] + lines[header_idx + 1:])
