"""Local, complete-ledger validation using the same checks as bean-check."""

from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from contextlib import contextmanager, nullcontext
from threading import Lock, RLock
import gc

from beancount import loader, __version__ as BEANCOUNT_VERSION
from beancount.ops import validation
from beancount.parser import printer
from .bot_utils import log, timed


# v2 temporarily changes the process working directory while parsing includes. v3 resolves paths without chdir and does not need this serialization.
_LOAD_GUARD = RLock() if BEANCOUNT_VERSION.startswith("2.") else nullcontext()


_gc_lock = Lock()
_gc_pauses = 0
_gc_restore = False


@contextmanager
def _gc_paused():
    """Loading allocates millions of long-lived objects; generational passes over them are pure overhead.

    The collector is process-wide, so overlapping loads share one pause and only the last one restores it. Nothing is skipped: garbage created meanwhile is collected once the collector resumes.
    """
    global _gc_pauses, _gc_restore
    with _gc_lock:
        if _gc_pauses == 0:
            _gc_restore = gc.isenabled()
            gc.disable()
        _gc_pauses += 1
    try:
        yield
    finally:
        with _gc_lock:
            _gc_pauses -= 1
            if _gc_pauses == 0 and _gc_restore:
                gc.enable()


def _error_details(errors):
    return "\n".join(printer.format_error(error).rstrip() for error in errors)


def _load_complete_file(filename):
    """Run the full loader and all bean-check validations without a disk cache."""
    # Disposable snapshots cannot reuse pickle caches. Keep the v2/v3 uncached entry point isolated here and covered by equivalence tests.
    with _LOAD_GUARD, _gc_paused():
        entries, errors, options = loader._uncached_load_file(filename, None, None, None)
        # Passing extra_validations to affected Beancount versions mutates their global validation list on every call. Run the same checks explicitly after the loader's basic checks.
        with timed("bean-check hardcore validation"):
            for check in validation.HARDCORE_VALIDATIONS:
                errors.extend(check(entries, options))
    return entries, errors, options


@contextmanager
def _materialized(texts):
    """Mirror one snapshot into a disposable directory so includes resolve as they do in the repository."""
    with TemporaryDirectory(prefix="ledger_check_") as directory:
        with timed("snapshot materialization"):
            for name, content in texts.items():
                path = PurePosixPath(name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(f"Invalid ledger path: {name}")
                target = Path(directory, *path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        yield directory


def load_ledger_texts(texts: dict[str, str], root: str, required_file: str | None = None):
    """Materialize one snapshot and return bean-check's entries/errors/options."""
    if root not in texts:
        raise ValueError(f"Missing ledger root: {root}")
    with _materialized(texts) as directory:
        with timed("bean-check parse and validation"):
            entries, errors, options = _load_complete_file(str(Path(directory, root)))
        # Keep diagnostics useful after the temporary snapshot is removed.
        for index, error in enumerate(errors):
            source = dict(error.source or {})
            if source.get("filename"):
                try:
                    source["filename"] = str(Path(source["filename"]).relative_to(directory))
                except ValueError:
                    pass
            errors[index] = error._replace(source=source)
        if errors:
            log(f"bean-check failed for {root} ({len(errors)} errors):\n{_error_details(errors)}")
        if required_file and str(Path(directory, required_file)) not in options.get("include", []):
            raise ValueError(f"Ledger root {root} does not include journal {required_file}.")
        return entries, errors, options


def check_ledger(texts: dict[str, str], root: str, required_file: str | None = None) -> tuple[list, dict]:
    """Mandatory commit/query gate: every bean-check error blocks the operation."""
    entries, errors, options = load_ledger_texts(texts, root, required_file)
    if errors:
        details = _error_details(errors)
        raise ValueError(f"bean-check failed ({len(errors)} errors):\n{details}")
    return entries, options
