# Contributing

1. Fork and create a branch from `main`.
2. `pip install -e ".[dev]"`
3. Make your change with tests. Run before opening a PR:
   ```bash
   ruff check . && ruff format .
   mypy src
   pytest
   ```
4. Keep PRs focused; describe the *why*. Do not include secrets or real user data.

Code style: type hints on public functions, English docstrings/comments, user-facing text in
Indonesian. Tests must not require network access or real API keys.
