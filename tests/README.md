# Test data privacy

Use synthetic fixtures only. Do not copy real journals, prompts, screenshots, names, account numbers, chat IDs, credentials, private URLs or deployment paths into tests, assertions, comments or expected output. Reproduce bugs with the smallest fictional example, retaining only the syntax and relationships needed for the regression. Use placeholder credentials and reserved `.invalid` domains.

The automated suites use explicit fake settings and mocked external services. Do not attach real config files, ledgers or test output containing personal data to commits or CI artifacts.

Run `../.venv/bin/python test_commands.py` from this directory to check command registration, retry behavior, help and startup dispatch. Its smoke test mocks Telegram HTTP and uses in-memory state; it never registers menus or sends messages to a real bot.

`test_analysis.py` also uses a loopback HTTP server and spawned BQL workers to exercise native tool calls and JSON fallback. It does not call an external model or read deployment configuration; these tests validate the protocol and bounds, not model reasoning quality.

`../scripts/benchmark_ledger.py` is an offline performance comparison using generated transactions and temporary files. It never loads deployment configuration or personal journals. The baseline and optimized loader each run in a separate process with identical basic-validation state before every sample, so repeated extra-validation registration does not inflate the baseline. Timing results are evidence for that environment, not a universal performance threshold.

`../scripts/preview_llm.py` is a manual integration preview, not an offline unit test. It requires `--live`, reads local configuration, the configured ledger and `user.md`, and sends input and context to the configured LLM. Use a dedicated test configuration and synthetic ledger; do not run it in CI. It does not write journals.

Cleaning current fixtures does not remove data from Git history. Any history rewrite needs separate coordination; rotate credentials if they were exposed.
