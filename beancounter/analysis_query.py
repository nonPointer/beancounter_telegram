"""Bounded, read-only queries over one already-validated ledger snapshot."""

import dataclasses
from datetime import date
from decimal import Decimal
import json
import multiprocessing
import re
import time

from beancount.core.amount import Amount
from beancount.core.data import Transaction
from beancount.core.inventory import Inventory
from beancount.core.position import Position

from .bot_utils import format_query_result
from .ledger import beancount_query
from .reports import _account_labels

try:
    from beancount.query import query_parser

    def parse_query(text):
        return query_parser.Parser().parse(text)
except ModuleNotFoundError as exc:
    if exc.name != "beancount.query":
        raise
    from beanquery.parser import parse as parse_query


MAX_ROWS = 100
MAX_RESULT_CHARS = 16000
QUERY_SECONDS = 15
COLUMNS = frozenset("date year month day account narration payee position weight number currency flag tags links balance other_accounts".split())
FUNCTIONS = frozenset("sum count abs neg root parent leaf year month day first last max min units cost currency number coalesce length round".split())


def _walk(node):
    yield node
    if dataclasses.is_dataclass(node):
        children = [getattr(node, f.name) for f in dataclasses.fields(node) if f.repr]
    elif isinstance(node, (list, tuple)):
        children = node
    else:
        children = ()
    for child in children:
        yield from _walk(child)


def validate_query(bql):
    if not isinstance(bql, str) or not bql.strip() or len(bql) > 4000:
        raise ValueError("BQL must be a non-empty string of at most 4000 characters")
    unquoted = re.sub(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' ''', "", bql, flags=re.VERBOSE)
    if ";" in unquoted:
        raise ValueError("Only one BQL statement is allowed; omit semicolons and comments")
    tree = parse_query(bql)
    if type(tree).__name__ != "Select" or tree.from_clause is not None:
        raise ValueError("Only SELECT without FROM is allowed")
    if not isinstance(tree.targets, list):
        raise ValueError("List explicit columns; SELECT * is not allowed")
    aliases = {target.name for target in tree.targets}
    alias_nodes = {id(node) for clause in (tree.group_by, tree.order_by) for node in _walk(clause)}
    for node in _walk(tree):
        name = type(node).__name__
        if name in ("Asterisk", "Wildcard"):
            raise ValueError("List explicit columns; SELECT * is not allowed")
        if name == "Column" and node.name not in COLUMNS and not (id(node) in alias_nodes and node.name in aliases):
            raise ValueError(f"Column not allowed: {node.name}")
        if name == "Function" and node.fname not in FUNCTIONS:
            raise ValueError(f"Function not allowed: {node.fname}")
    return tree


def encode_value(value):
    """Keep decimals exact and inventories explicitly denominated; never stringify metadata."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, Amount):
        return {"number": str(value.number), "currency": value.currency}
    if isinstance(value, Position):
        result = {"units": encode_value(value.units)}
        if value.cost is not None:
            result["cost"] = {"number": str(value.cost.number), "currency": value.cost.currency}
        return result
    if isinstance(value, Inventory):
        return {"positions": [encode_value(p) for p in value.get_positions()]}
    if isinstance(value, (set, frozenset, list, tuple)):
        return [encode_value(v) for v in sorted(value, key=str)]
    raise ValueError(f"Unsupported result type: {type(value).__name__}")


def query_result(loaded, bql):
    validate_query(bql)
    types, rows = beancount_query.run_query(*loaded, bql)
    if len(types) > 16:
        raise ValueError("At most 16 result columns are allowed")
    columns = [str(c[0])[:160] for c in types]
    kept, display = [], []
    size, clipped = len(json.dumps(columns)), any(len(str(c[0])) > 160 for c in types)
    for row in rows[:MAX_ROWS]:
        encoded, shown = [], []
        for value in row:
            cell = encode_value(value)
            if len(json.dumps(cell, ensure_ascii=False)) > 800:
                cell = {"truncated": True}
                shown.append("[单元格过长，已省略]")
                clipped = True
            else:
                shown.append(str(value) if isinstance(value, Decimal) else value)
            encoded.append(cell)
        row_size = len(json.dumps(encoded, ensure_ascii=False))
        if size + row_size > MAX_RESULT_CHARS - 1000:
            clipped = True
            break
        size += row_size
        kept.append(encoded)
        display.append(shown)
    # Shorten only exact known account cells, not payees/narrations containing colons.
    accounts = {p.account for e in loaded[0] if isinstance(e, Transaction) for p in e.postings}
    labels = _account_labels(accounts)
    for row in display:
        for i, value in enumerate(row):
            if columns[i] == "account" and isinstance(value, str):
                row[i] = labels.get(value, value)
    truncated = clipped or len(kept) < len(rows)
    table = format_query_result([(c, str) for c in columns], display, max_chars=2400)
    if truncated:
        table += f"\n[结果不完整：显示 {len(kept)}/{len(rows)} 行，可能含省略单元格]"
    return {"columns": columns, "rows": kept, "total_rows": len(rows), "truncated": truncated}, table


def _query_worker(connection, loaded):
    try:
        while True:
            bql = connection.recv()
            try:
                connection.send((True, query_result(loaded, bql)))
            except Exception as exc:
                connection.send((False, f"{type(exc).__name__}: {exc}"[:600]))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class QuerySession:
    """One spawn worker per analysis; a timed-out BQL can be terminated, not left running."""

    def __init__(self, loaded):
        self.loaded = loaded
        self.process = None
        self.connection = None

    def __enter__(self):
        # Strip metadata without mutating the shared cache. Queries cannot access it even through functions.
        entries, options = self.loaded
        clean = []
        for entry in entries:
            entry = entry._replace(meta={})
            if isinstance(entry, Transaction):
                entry = entry._replace(postings=[p._replace(meta=None) for p in entry.postings])
            clean.append(entry)
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_query_worker, args=(child, (clean, options)), daemon=True)
        try:
            self.process.start()
        except BaseException:
            self.connection.close()
            raise
        finally:
            child.close()
        return self

    def query(self, bql, deadline):
        timeout = min(QUERY_SECONDS, deadline - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("Analysis time budget exhausted")
        self.connection.send(bql)
        if not self.connection.poll(timeout):
            raise TimeoutError("BQL query timed out")
        ok, result = self.connection.recv()
        if not ok:
            raise ValueError(result)
        return result

    def __exit__(self, *exc):
        self.connection.close()
        # Termination also covers a query stuck in a regex or an oversized aggregation.
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=2)
        if self.process.is_alive():
            self.process.kill()
            self.process.join()
        self.process.close()
