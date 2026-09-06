"""Local, complete-ledger validation using the same checks as bean-check."""

from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from beancount import loader
from beancount.ops import validation


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
        if required_file and str(Path(directory, required_file)) not in options.get("include", []):
            raise ValueError(f"Ledger root {root} does not include journal {required_file}.")
        return entries, errors, options


def check_ledger(texts: dict[str, str], root: str, required_file: str | None = None) -> tuple[list, dict]:
    """Mandatory commit/query gate: every bean-check error blocks the operation."""
    entries, errors, options = load_ledger_texts(texts, root, required_file)
    if errors:
        details = "\n".join(str(error.message) for error in errors[:8])
        raise ValueError(f"bean-check failed ({len(errors)} errors):\n{details}")
    return entries, options
