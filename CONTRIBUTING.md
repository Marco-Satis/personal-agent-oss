# Contributing

Thanks for your interest! The project is young, so ideas and feedback count as much as code.

## Good first steps

- Try `personal-agent simulate` and tell us what felt wrong.
- Open an issue for a bug, a missing feature or a question about the design.
- For larger changes, open an issue first so we can agree on the approach.

## Ground rules

1. **Keep it small.** v1 uses only the Python standard library. A new dependency needs a good reason
   (and is better as an optional extra).
2. **Respect the security model** in [docs/CONCEPT.md](docs/CONCEPT.md#security-model). In short: text
   from sessions, mails, web pages or callers is data and never chooses an action; nothing is sent
   without approval; no secrets in code, logs or tests.
3. **Tests.** New behaviour comes with tests; `python3 -m pytest` must pass. Tests must never place a
   real call or touch a real account.
4. **No personal data** in issues, commits or test fixtures: use example numbers and names.

## Development setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

## Pull requests

- One topic per pull request, with a short description of what and why.
- Code and comments in English. Issues and pull requests in English or German.
- By contributing you agree that your contribution is licensed under the
  [Apache License 2.0](LICENSE).

## Security issues

Please report them privately, see [SECURITY.md](SECURITY.md).
