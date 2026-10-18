# personal-agent

> Working title. A self-hosted personal agent for **Claude Code** that **calls you on the phone** when
> something important happens — and lets you ask what your sessions are doing.

**Status: v0.1, early.** The phone call and the session status work. Session control, e-mail and a
dashboard are on the [roadmap](docs/CONCEPT.md#roadmap).

## What v0.1 does

- Your Claude Code sessions report their state (waiting / working / idle) through a small hook.
- A script, cron job or the built-in watch raises a **critical** event — for example "a session has been
  waiting for input for 20 minutes" or "the nightly backup failed".
- The agent **calls your phone**. After your PIN it reads the alert and offers a menu:
  - **1 / "status"** – which sessions are waiting, working or idle, and for how long
  - **2** – repeat the alert
  - **3** – acknowledge (no more calls about it today)
  - **9 / "goodbye"** – hang up
- Built-in limits keep it quiet: only critical events call, one call per key per hour, minimum interval,
  daily cap, quiet hours.

English and German voice prompts are included.

## Why another agent?

There are voice assistants and there are session managers, but nothing that **rings your phone when your
agents need you** and lets you check on them from there. This project tries to do that with very little
code: v0.1 uses only the Python standard library.

## Quick start (no phone, no cost)

```bash
git clone <this repo> && cd <repo>
python3 -m pip install -e .            # Python 3.10+

# Play a whole call in the terminal:
PA_CALL_PIN=123456 personal-agent simulate --summary "Nightly backup failed."
```

Type `123456#` for the PIN, then `1`, `status`, `2`, `3` or `9`.

## Run the server

```bash
cp .env.example .env    # fill it in; never commit it
set -a; . ./.env; set +a
personal-agent serve    # listens on 127.0.0.1:8787, provider "fake" places no real call
```

Raise an event from anywhere on the same machine:

```bash
personal-agent escalate backup-failed "The nightly backup failed." --source cron
```

See what the agent sees:

```bash
personal-agent status
```

## Talk to the assistant (preview)

The assistant runs on your own Claude subscription through the unmodified Claude Code CLI, locked down:
restricted mode (no shell, file tools only in its own workspace), an explicit tool allowlist, only its own
MCP servers, and no API keys in its environment, so it never falls back to paid API usage.

```bash
claude setup-token                       # once; keep the token in your password manager
export CLAUDE_CODE_OAUTH_TOKEN=...       # or PA_CLAUDE_CONFIG_DIR pointing at an existing login
personal-agent chat                      # read-only tools for now; approvals for writing come next
```

| Variable | Default | Meaning |
|---|---|---|
| `PA_CLAUDE_MODEL` | `sonnet` | model alias |
| `PA_WORKDIR` | `$PA_STATE_DIR/workspace` | the only folder the assistant's file tools can touch |
| `PA_CLAUDE_CONFIG_DIR` | `$PA_STATE_DIR/claude-config` | separate Claude Code config, never your `~/.claude` by default |
| `PA_CLAUDE_BIN` | `claude` | path to the Claude Code CLI |

## Connect your Claude Code sessions

Add the status hook to `~/.claude/settings.json` (adjust the path):

```json
{
  "hooks": {
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 /path/to/hooks/claude_status_hook.py" }] }],
    "PreToolUse":       [{ "hooks": [{ "type": "command", "command": "python3 /path/to/hooks/claude_status_hook.py" }] }],
    "Notification":     [{ "hooks": [{ "type": "command", "command": "python3 /path/to/hooks/claude_status_hook.py" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python3 /path/to/hooks/claude_status_hook.py" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "python3 /path/to/hooks/claude_status_hook.py" }] }]
  }
}
```

Set `PA_WAITING_ESCALATE_MIN=20` to get a call when a session has waited 20 minutes for you.

## Real phone calls (Twilio)

1. Create a Twilio account and a phone number. Check the regulatory requirements for numbers in your
   country first.
2. Set `PA_PROVIDER=twilio`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` and
   `PA_PUBLIC_URL` (an `https://` URL).
3. Expose **only** the `/twilio/` path to the internet through a reverse proxy or tunnel with TLS.
   `/escalate` must stay local.

Calls cost money (a few cents per minute plus the number). The limits above keep that small.

## Security

This project is designed for an agent that will eventually read mail and steer coding sessions, so it is
built against prompt injection from the start: data never becomes a command, every send needs your
approval, roles get separate rights, callers must enter a PIN, webhooks are signature-checked, the server
binds to `127.0.0.1`. Details in [docs/CONCEPT.md](docs/CONCEPT.md#security-model).
Report vulnerabilities as described in [SECURITY.md](SECURITY.md).

## Development

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest
```

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache-2.0](LICENSE)
