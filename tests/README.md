# Test data privacy

Use synthetic fixtures only. Do not copy real journals, prompts, screenshots, names, account numbers, chat IDs, credentials, private URLs or deployment paths into tests, assertions, comments or expected output. Reproduce bugs with the smallest fictional example, retaining only the syntax and relationships needed for the regression. Use placeholder credentials and reserved `.invalid` domains.

The automated suites use explicit fake settings and mocked external services. Do not attach real config files, ledgers or test output containing personal data to commits or CI artifacts.

`../scripts/preview_llm.py` is a manual integration preview, not an offline unit test. It requires `--live`, reads local configuration, the configured ledger and `user.md`, and sends input and context to the configured LLM. Use a dedicated test configuration and synthetic ledger; do not run it in CI. It does not write journals.

Cleaning current fixtures does not remove data from Git history. Any history rewrite needs separate coordination; rotate credentials if they were exposed.
