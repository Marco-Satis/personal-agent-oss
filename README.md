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
personal-agent chat
```

The assistant may read, search, write and edit files in its workspace. **Writing asks you first:**

```
[approval] level 2 (write): Write
  file_path="notes/a.md", content=11 chars: "hello world"
Allow? [y/N]
```

- **Level 1 (read)**: reading inside the workspace, allowed without asking.
- **Level 2 (write)**: answer `y` to allow; anything else denies.
- **Level 3 (danger)**: tools that could send, delete, pay or publish, files such as `CLAUDE.md`,
  and anything that cannot be classified safely. After `y` you must also type `confirm`.
- Never allowed, no prompt: paths outside the workspace, and writes to `.git/`, `.claude/`,
  `.mcp.json`, `.envrc`, `.vscode/` and similar configuration paths.

No answer within `PA_APPROVAL_TIMEOUT_S`, a closed terminal or any internal error means **deny**.

Every reply shows which tier answered it:

- `agent[1]>`: `help`, `status` (your Claude Code sessions), `quota` and `quota reset` are answered
  without any model.
- `agent[2]>`: short, self-contained questions ("What is the capital of France?") go to a small model
  in a separate session **with no tools**.
- `agent[3]>`: everything else goes to the main assistant. Start a message with `!` to force this tier.

**Quota stop.** Before each turn the agent checks the subscription usage Claude Code last reported. If
the five-hour or seven-day window is at `PA_QUOTA_STOP` or above, the status is not `allowed`, or paid
overage is in use, the message is **not sent** (`[quota] Not sent: ...`) until the window resets. When
paid overage was seen and no reset time is known, it stays blocked until you switch overage off and
type `quota reset`. A session that reports an API key instead of the subscription login is stopped. As
the hard backstop, also switch off extra or overage usage in your Claude account settings.

The workspace must not contain anything the agent runs or trusts: the agent's own package, the Python
interpreter and its site-packages, the Claude Code binary and config, `PA_STATE_DIR` or
`PA_STATUS_DIR`. `chat` refuses to start otherwise.

| Variable | Default | Meaning |
|---|---|---|
| `PA_CLAUDE_MODEL` | `sonnet` | model alias |
| `PA_WORKDIR` | `$PA_STATE_DIR/workspace` | the only folder the assistant's file tools can touch |
| `PA_CLAUDE_CONFIG_DIR` | `$PA_STATE_DIR/claude-config` | separate Claude Code config, never your `~/.claude` by default |
| `PA_CLAUDE_BIN` | `claude` | path to the Claude Code CLI |
| `PA_APPROVAL_TIMEOUT_S` | `300` | seconds to answer an approval prompt (10–3600) before it is denied |
| `PA_QUOTA_STOP` | `0.9` | stop sending at this share of a usage window (greater than 0, at most 1) |
| `PA_ROUTER_SMALL_MODEL` | `haiku` | model alias for tier 2; `off` sends everything to the main assistant |
| `PA_ROUTER_SMALL_MAX_CHARS` | `160` | longest message tier 2 may take (20–2000) |

### Approvals from your phone

Not at the terminal? `personal-agent chat --remote-approvals` sends each approval to your phone instead:
a short Discord notice with the tool name, the level and a one-time link. The link opens a small page
served by `personal-agent serve`, where you allow or deny. The notice never contains the tool input.

1. Create a Discord webhook for a private channel and set `PA_DISCORD_WEBHOOK` (treat it as a secret).
2. Set `PA_PUBLIC_APPROVAL_URL` to the address your phone uses to reach the server, for example your
   tailnet name. Forward **only** `/approve/` (with the trailing slash) to `127.0.0.1:8787`. Never
   forward `/internal/` or `/escalate`; prefer tailnet-only access over a public URL.
3. Set `PA_APPROVAL_PIN` (at least 6 digits). Level 3 needs the typed `confirm` **and** this PIN on the
   page; three wrong PINs deny the request.
4. Run `personal-agent serve` (same `PA_API_TOKEN`), then `personal-agent chat --remote-approvals`.

Each link works once and expires shortly before `PA_APPROVAL_TIMEOUT_S`. Expired, cancelled or
unanswered requests are denied. At most five requests may be pending and at most five notices are sent
per ten minutes.

| Variable | Default | Meaning |
|---|---|---|
| `PA_DISCORD_WEBHOOK` | – | Discord webhook URL (`https://discord.com/api/webhooks/...`), never logged |
| `PA_PUBLIC_APPROVAL_URL` | – | base URL of the approval page, `https://` is added if missing |
| `PA_APPROVAL_PIN` | – | PIN for level-3 approvals on the page, required when remote approvals are set |
| `PA_TRUSTED_CLIENTS` | – | extra private networks allowed on `/escalate` and `/internal/`, e.g. `172.17.0.1/32` when `serve` runs in a container (host clients then arrive from the bridge gateway and otherwise get 404) |

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
   `/escalate` and `/internal/` must stay local.

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
