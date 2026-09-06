"""Local, complete-ledger validation using the same checks as bean-check."""

from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from beancount import loader
from beancount.ops import validation
from beancount.parser import printer
from .bot_utils import log


def _error_details(errors):
    return "\n".join(printer.format_error(error).rstrip() for error in errors)


def load_ledger_texts(texts: dict[str, str], root: str, required_file: str | None = None):
    """Materialize one snapshot and return bean-check's entries/errors/options."""
    if root not in texts:
        raise ValueError(f"Missing ledger root: {root}")
    with TemporaryDirectory(prefix="ledger_check_") as directory:
        for name, content in texts.items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"Invalid ledger path: {name}")
            target = Path(directory, *path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        entries, errors, options = loader.load_file(
            str(Path(directory, root)),
            extra_validations=validation.HARDCORE_VALIDATIONS,
        )
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
