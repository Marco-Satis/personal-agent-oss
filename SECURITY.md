# Security Policy

This project places phone calls and will later read e-mail and steer Claude Code sessions. Security
reports are very welcome.

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's
[private vulnerability reporting](../../security/advisories/new) for this repository instead.

Include what you found, how to reproduce it and what an attacker could achieve. You will get an answer
within a week. Fixes are released as soon as possible and credited if you wish.

## Supported versions

Only the latest release receives fixes while the project is below 1.0.

## Scope

Especially interesting:

- Ways for text from sessions, events, e-mails or callers to trigger an action (prompt or command injection)
- Bypassing the caller PIN, the webhook signature check or the bearer token
- Ways to make the agent call more often than its limits allow
- Secrets, PINs or full phone numbers ending up in logs or files

## Design rules

The security model is described in [docs/CONCEPT.md](docs/CONCEPT.md#security-model). Changes that
weaken one of its rules need a clear reason in the pull request.
