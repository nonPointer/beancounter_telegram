"""Compare complete-ledger checks or payee-history lookup on synthetic data."""

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import beancount
from beancount import loader
from beancount.ops import validation
from beancount.core.data import Transaction
from beancounter import ledger_validation
from beancounter.ledger import LedgerMixin


def synthetic_ledger(count):
    texts = {"main.bean": "2000-01-01 open Assets:Cash GBP\n2000-01-01 open Expenses:Food GBP\n"}
    for index in range(50):
        name = f"journal/{index:02d}.bean"
        texts["main.bean"] += f'include "{name}"\n'
        texts[name] = "".join(f'2020-01-01 * "Synthetic Cafe" "Example {number}"\n  Assets:Cash -1 GBP\n  Expenses:Food 1 GBP\n' for number in range(index, count, 50))
    return texts


def measure(mode, count, repeats):
    texts = synthetic_ledger(count)
    baseline_validations = list(validation.VALIDATIONS)
    optimized = ledger_validation._load_complete_file
    previous = lambda filename: loader.load_file(filename, extra_validations=validation.HARDCORE_VALIDATIONS)
    samples, cache_samples, validator_counts = [], [], []
    real_dump = loader.pickle.dump
    with patch("beancounter.bot_utils.log"), patch("beancounter.ledger_validation.log"):
        # Warm imports equally, without priming a reusable snapshot or growing the validation list.
        ledger_validation.load_ledger_texts({"main.bean": "; warm imports\n"}, "main.bean")
        with patch.object(ledger_validation, "_load_complete_file", previous if mode == "baseline" else optimized):
            for _ in range(repeats):
                with patch.object(validation, "VALIDATIONS", list(baseline_validations)), patch.object(loader.pickle, "dump", wraps=real_dump) as dumped:
                    started = time.perf_counter()
                    entries, errors, _ = ledger_validation.load_ledger_texts(texts, "main.bean", "main.bean")
                    samples.append(time.perf_counter() - started)
                    assert not errors, errors
                    assert len(entries) == count + 2, len(entries)
                    validator_counts.append(len(validation.VALIDATIONS))
                    cache_samples.append(dumped.call_count)
                    del entries
    return {"mode": mode, "beancount": beancount.__version__, "python": platform.python_version(),
            "platform": platform.system(), "transactions": count, "files": len(texts),
            "seconds": [round(value, 4) for value in samples], "median_seconds": round(statistics.median(samples), 4),
            "pickle_writes": cache_samples, "validators_before": len(baseline_validations), "validators_after": validator_counts}


def measure_history(count, repeats):
    """Time just retrieval/rendering; fixture creation and memory checks are separate."""
    seed = Transaction({}, date(2020, 1, 1), "*", "Synthetic Cafe", "Example", frozenset(), frozenset(), [])
    entries = [seed._replace(date=seed.date + timedelta(days=index // 500),
                            payee="Synthetic Cafe" if index % 10 else "Synthetic Shop",
                            narration=f"Example {index}") for index in range(count)]
    ledger = LedgerMixin()  # No config, state, network or ledger loading.

    def baseline(payee):
        needle = payee.casefold().strip()
        matched = [entry for entry in entries if isinstance(entry, Transaction) and entry.payee
                   and (needle in entry.payee.casefold() or entry.payee.casefold() in needle)]
        matched.sort(key=lambda entry: entry.date)
        return "\n\n".join(ledger._format_example_entry(entry) for entry in matched[-10:]) or None

    optimized = lambda payee: ledger.examples_for_payee(payee, loaded=(entries, {}))
    for payee in ("Synthetic Cafe", "Synthetic Shop", "Absent Payee"):
        assert baseline(payee) == optimized(payee)
        results = {"benchmark": "payee_history", "python": platform.python_version(),
                   "beancount": beancount.__version__, "transactions": count, "payee": payee}
        for name, lookup in (("baseline", baseline), ("optimized", optimized)):
            samples = []
            for _ in range(repeats):
                started = time.perf_counter()
                lookup(payee)
                samples.append(time.perf_counter() - started)
            tracemalloc.start()
            lookup(payee)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            results[name] = {"median_ms": round(statistics.median(samples) * 1000, 4),
                             "peak_kib": round(peak / 1024, 2)}
        print(json.dumps(results), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transactions", type=int, default=100000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--history", action="store_true", help="Compare bounded payee-history lookup instead of validation")
    parser.add_argument("--mode", choices=("baseline", "optimized"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.transactions < 1 or args.repeats < 1:
        parser.error("transactions and repeats must be positive")
    if args.history:
        measure_history(args.transactions, args.repeats)
        return
    if args.mode:
        print(json.dumps(measure(args.mode, args.transactions, args.repeats)))
        return
    results = []
    for mode in ("baseline", "optimized"):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--mode", mode,
                                 "--transactions", str(args.transactions), "--repeats", str(args.repeats)],
                                capture_output=True, text=True, check=True)
        measured = json.loads(result.stdout)
        results.append(measured)
        print(json.dumps(measured), flush=True)
    print(json.dumps({"median_reduction_percent": round(100 * (1 - results[1]["median_seconds"] / results[0]["median_seconds"]), 1)}))


if __name__ == "__main__":
    main()
