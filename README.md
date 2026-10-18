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
restricted mode (no shell, file tools only in its own vault), an explicit tool allowlist, only its own
MCP servers, and no API keys in its environment, so it never falls back to paid API usage.

```bash
claude setup-token                       # once; keep the token in your password manager
export CLAUDE_CODE_OAUTH_TOKEN=...       # or PA_CLAUDE_CONFIG_DIR pointing at an existing login
personal-agent chat
```

The assistant may read, search, write and edit files in its [vault](#the-vault). **Writing asks you first:**

```
[approval] level 2 (write): Write
  file_path="notes/a.md", content=11 chars: "hello world"
Allow? [y/N]
```

- **Level 1 (read)**: reading inside the vault, allowed without asking.
- **Level 2 (write)**: answer `y` to allow; anything else denies.
- **Level 3 (danger)**: tools that could send, delete, pay or publish, files such as `CLAUDE.md`,
  and anything that cannot be classified safely. After `y` you must also type `confirm`.
- Never allowed, no prompt: paths outside the vault, and writes to `.git/`, `.claude/`,
  `.mcp.json`, `.envrc`, `.vscode/` and similar configuration paths.

No answer within `PA_APPROVAL_TIMEOUT_S`, a closed terminal or any internal error means **deny**.

Every reply shows which tier answered it:

- `agent[1]>`: `help`, `status` (your Claude Code sessions), `quota`, `quota reset` and
  `note <text>` are answered without any model.
- `agent[2]>`: short, self-contained questions ("What is the capital of France?") go to a small model
  in a separate session **with no tools**.
- `agent[3]>`: everything else goes to the main assistant. Start a message with `!` to force this tier.

**Quota stop.** Before each turn the agent checks the subscription usage Claude Code last reported. If
the five-hour or seven-day window is at `PA_QUOTA_STOP` or above, the status is not `allowed`, or paid
overage is in use, the message is **not sent** (`[quota] Not sent: ...`) until the window resets. When
paid overage was seen and no reset time is known, it stays blocked until you switch overage off and
type `quota reset`. A session that reports an API key instead of the subscription login is stopped. As
the hard backstop, also switch off extra or overage usage in your Claude account settings.

The vault must not contain anything the agent runs or trusts: the agent's own package, the Python
interpreter and its site-packages, the Claude Code binary and config, `PA_STATE_DIR` or
`PA_STATUS_DIR`, the plugins directory, the core plugin or the state files. It must not lie inside the
status, Claude Code config, plugins, core plugin or approval runtime directory either, nor inside the
source checkout the core plugin was found in (the default `$PA_STATE_DIR/vault` is fine).
`chat` and `vault init` refuse to start otherwise.

| Variable | Default | Meaning |
|---|---|---|
| `PA_CLAUDE_MODEL` | `sonnet` | model alias |
| `PA_VAULT_DIR` | `$PA_STATE_DIR/vault` | the vault, the only folder the assistant's file tools can touch; `PA_WORKDIR` is still accepted as an alias |
| `PA_VAULT_CONTEXT_CHARS` | `3000` | character budget for note excerpts attached to tier-3 messages (0–12000, `0` turns them off) |
| `PA_CORE_PLUGIN_DIR` | `plugins/core` of a source checkout | the shipped core plugin; `off` disables it |
| `PA_CLAUDE_CONFIG_DIR` | `$PA_STATE_DIR/claude-config` | separate Claude Code config, never your `~/.claude` by default |
| `PA_CLAUDE_BIN` | `claude` | path to the Claude Code CLI |
| `PA_APPROVAL_TIMEOUT_S` | `300` | seconds to answer an approval prompt (10–3600) before it is denied |
| `PA_QUOTA_STOP` | `0.9` | stop sending at this share of a usage window (greater than 0, at most 1) |
| `PA_ROUTER_SMALL_MODEL` | `haiku` | model alias for tier 2; `off` sends everything to the main assistant |
| `PA_ROUTER_SMALL_MAX_CHARS` | `160` | longest message tier 2 may take (20–2000) |

### The vault

The assistant works in a Markdown vault. `personal-agent vault init` creates the skeleton (`chat` does
it too on start); existing files are never overwritten, and symlinks are left untouched:

```
CLAUDE.md               structure rules, so the vault does not sprawl
heute.md                today's focus: Top 3 and notes
raw/                    captured, unprocessed notes: raw/inbox-YYYY-MM-DD.md
wiki/                   organized articles, one topic per file, listed in wiki/_master-index.md
outputs/                generated results such as briefs
```

`personal-agent vault path` prints where it is. **Moved:** earlier versions used
`$PA_STATE_DIR/workspace`; move your files to `$PA_STATE_DIR/vault` or point `PA_VAULT_DIR` at the old
folder.

- `note <text>` (also `notiz <text>`) appends `- HH:MM <text>` to today's `raw/inbox-YYYY-MM-DD.md`
  without any model and without using quota. Control and invisible characters are removed.
- For messages that go to the main assistant, the agent picks the most relevant paragraphs from `wiki/`
  and `heute.md` (shared words, file names, recently changed files; never `raw/` or `outputs/`) and
  sends them in front of your message inside a `<vault-excerpts>` block, escaped and marked as
  reference data, never instructions. The reply shows `[context: N notes]`, and an approval prompt in
  such a turn says that the request may come from note text. Tier 2 never gets notes. A turn that runs
  a plugin command (`/core:organize-inbox`) is marked the same way.
- Messages starting with `/` are refused unless they name an allowed plugin command, because Claude
  Code would expand them as slash commands (`/config`, `/clear`, ...). This also covers `!/...`.

### Plugins

Extensions are standard Claude Code plugins: a folder with `.claude-plugin/plugin.json` plus `skills/`,
`commands/` and `agents/` Markdown files. The core plugin ships with the repository in `plugins/core`
and is found automatically in a source checkout; a wheel install does not include it, so point
`PA_CORE_PLUGIN_DIR` at a copy (or set it to `off`).

The core plugin holds three skills for the vault. Each one works only inside the vault with Read, Glob,
Grep, Write and Edit, treats note text as data, and every write still asks for your approval:

| Skill | What it does |
|---|---|
| `/core:organize-inbox` | files open `raw/` entries into `wiki/` articles, updates `wiki/_master-index.md` and marks each entry `→ filed: wiki/<file>.md` (raw text is never deleted) |
| `/core:daily-plan` | reads `heute.md`, the index, open `- [ ]` tasks and the newest inbox, and writes the top 3 priorities to `heute.md` |
| `/core:capture-note <text>` | appends `- HH:MM <text>` to today's inbox; `note <text>` does the same without a model |

You can type them, or ask in plain words ("plan my day") and the assistant loads the skill itself.

```bash
personal-agent plugins check ./my-plugin     # findings only, nothing is changed
personal-agent plugins enable ./my-plugin    # shows the skills and asks; --yes, --replace
personal-agent plugins list                  # core (shipped) first, then the enabled ones
personal-agent plugins disable my-plugin
```

`enable` copies the checked files (never a symlink) to `$PA_STATE_DIR/plugins/<name>/` and records their
digest in `$PA_STATE_DIR/plugins.json`. `chat` checks every plugin again on each start, skips one that
changed since it was enabled, and passes each plugin with its own `--plugin-dir`. Skills appear as
`/<plugin>:<skill>` and the assistant may use them through the Skill tool; built-in Claude Code skills
are denied.

**Accepted:** skills (exactly one self-contained `SKILL.md` each: the plugin folder lies outside the vault,
so the assistant cannot read supporting files), commands and agents as `.md` files, a README, LICENSE and
CHANGELOG. **Refused, without an override:**

- hooks (`hooks/`, a `hooks` key, `hooks:` in frontmatter): in a test with Claude Code 2.1.302 plugin
  hooks ran arbitrary commands even under `--restricted`, with the session environment and login;
- MCP and language servers (`.mcp.json`, `.lsp.json`, `mcpServers`, `lspServers`): they start as soon as
  `--strict-mcp-config` is missing;
- `allowed-tools`, `model`, `tools` and `permissionMode` in frontmatter, unknown manifest or frontmatter
  keys, YAML features a simple parser could read differently (tabs, anchors, flow syntax, duplicate keys);
- symlinks, hard links, executable files, binaries, shebangs, hidden files, odd file names, more than 200
  files, files over 64 KiB, more than 1 MiB in total, more than 20 skills, commands and agents;
- credentials, URLs and network tools in skill, command and agent text, encoded blobs, `eval`/`exec`,
  shell expansion (``!`...` ``, ```` ```! ````, `$(`, `${`), `@file` references (also quoted), invisible or bidi
  characters, and descriptions that try to override instructions.

Skill descriptions are part of every request, so all loaded plugins together may use at most 4 KiB of
descriptions. As defense in depth every Claude Code session runs with hooks disabled
(`disableAllHooks`), with every Skill call routed through the approval broker, and with
`--strict-mcp-config`; a turn that still shows a hook, an unexpected plugin, MCP server, tool or skill is
stopped (`[safety] Stopped: ...`) and the chat must be restarted. The plugin names `core` and `cc-*`
are reserved. `$PA_STATE_DIR` and its `plugins/` folder must be owned by you and not writable by group
or others; `enable` tightens a state folder it finds group-writable.

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

### Morning brief

`personal-agent brief` builds today's brief **without any model**: the date, the focus from `heute.md`,
open `- [ ]` tasks from `heute.md` and `wiki/` (at most 15; `raw/` is unprocessed capture and stays
out), a summary of your Claude Code session status, the quota status and the number of new captures
since the last brief. It prints the brief and writes `outputs/brief-YYYY-MM-DD.md` in the vault.

```bash
personal-agent brief             # print and write the file
personal-agent brief --send      # also post it to PA_DISCORD_WEBHOOK
personal-agent brief --polish    # let the small model rewrite the text (see below)
personal-agent brief --quiet     # write only
```

With `PA_BRIEF_TIME=07:30`, `personal-agent serve` sends the brief once a day at that local time
(`PA_DISCORD_WEBHOOK` alone is enough; remote approvals are not needed). The day is recorded in
`$PA_STATE_DIR/brief.json`, so a restart, a crash or an earlier manual `brief --send` never causes a
second post. A failed post is retried at most three times, at least five minutes apart. After downtime
the brief is only sent while the grace window (`PA_BRIEF_GRACE_MIN`) is still open; missed days are
never caught up. Local time comes from the process: set `TZ` (e.g. `TZ=Europe/Berlin`) in a container.

**Privacy:** by default Discord only gets counts (`PA_BRIEF_DISCORD=counts`): number of open tasks and
focus lines, session states, quota and new captures, plus the path of the full brief in the vault. Your
note and task text stays on your machine. With `full`, the whole brief is posted; every text from notes
and status files is escaped (no links, no mentions, no formatting) and URLs are shown as `https[:]//`.
Choose `full` only if you are fine with that text passing through Discord.

`--polish` (or `PA_BRIEF_POLISH=1` for the scheduled brief) asks the tier-2 small model, with no tools,
no plugins and no saved transcript, to rewrite the deterministic brief for readability. It runs at most
once per day, only when the quota guard allows a turn, and its result is used only if it adds no URL, no
domain, no `@` and no number that the deterministic brief does not contain; otherwise the deterministic
brief is used. The polished text is kept in `$PA_STATE_DIR/brief-polished.json` for retries and
written to `outputs/brief-YYYY-MM-DD.polished.md` as output only: that file is never read back, because
the assistant can write the vault. The scheduled brief prints nothing, so `PA_BRIEF_POLISH` only takes
effect together with `PA_BRIEF_DISCORD=full`; in `counts` mode it would spend a model call every day on
text nobody sees, and is skipped.

If a post fails halfway, the exact chunks of that attempt are kept in `$PA_STATE_DIR/brief-chunks.json`
(0600, removed after a successful send) and a retry posts only the chunks that did not go out, even
when the quota or session status changed in between.

| Variable | Default | Meaning |
|---|---|---|
| `PA_BRIEF_TIME` | – | daily brief time `HH:MM` (local), empty = off; needs `PA_DISCORD_WEBHOOK` |
| `PA_BRIEF_GRACE_MIN` | `120` | minutes after `PA_BRIEF_TIME` in which a late or retried send is still allowed (1–720) |
| `PA_BRIEF_POLISH` | off | `1`/`true`/`yes`: polish the scheduled brief with the small model (only with `PA_BRIEF_DISCORD=full`) |
| `PA_BRIEF_DISCORD` | `counts` | `counts` sends only counts to Discord, `full` the escaped brief text |

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
