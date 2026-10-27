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
- Optional [Telegram bot](#telegram): notices, approvals with buttons and a chat with the assistant from
  your phone, bound to your account only.

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

The assistant runs through the unmodified Claude Code CLI, locked down: restricted mode (no shell, file
tools only in its own vault), an explicit tool allowlist, only its own MCP servers, and telemetry and error
reporting switched off.

Each installation uses **exactly one access path** (`PA_ACCESS_MODE`):

| Mode | For | Credential | Guard |
|---|---|---|---|
| `subscription` (default) | your own personal use | your Claude login (`CLAUDE_CODE_OAUTH_TOKEN` or `PA_CLAUDE_CONFIG_DIR`) | quota guard: stops before the plan's usage windows run out; an API key never reaches the CLI, so it never falls back to paid usage |
| `api` | a business, or anything run for other people | `ANTHROPIC_API_KEY` from the business's own Claude Console | spend guard: daily and monthly caps in USD (`PA_SPEND_CAP_DAY_USD`, default 3; `PA_SPEND_CAP_MONTH_USD`, default 40), notice at 50/80/100 % |

Setting both credentials in `api` mode is a configuration error. A subscription login is for your own
use; anything you run for a business or for other people needs an API key under Anthropic's commercial
terms. Also set a workspace spend limit in the Claude Console: the guard can overrun its cap by one turn,
because a turn's cost is known only when it ends.

```bash
claude setup-token                       # once; keep the token in your password manager
export CLAUDE_CODE_OAUTH_TOKEN=...       # or PA_CLAUDE_CONFIG_DIR pointing at an existing login
personal-agent chat
personal-agent modules                   # access mode, usage, and which modules run
```

**Modules.** The core always runs; the brief, background tasks, domains and the Telegram bot are modules.
`PA_MODULES=brief,telegram` runs only those (default: all; `none` runs the core alone). A module switched
off here wins over its own settings.

The assistant may read, search, write and edit files in its [vault](#the-vault). **Writing asks you first:**

```
[approval · chat] level 2 (write): Write
  file_path="notes/a.md", content=11 chars: "hello world"
Allow? [y/N]
```

The part after `approval ·` says who asks: `chat` for the main assistant, `task 3f2a9c (research)` for a
[background helper](#background-tasks).

- **Level 1 (read)**: reading inside the vault, allowed without asking.
- **Level 2 (write)**: answer `y` to allow; anything else denies.
- **Level 3 (danger)**: tools that could send, delete, pay or publish, files such as `CLAUDE.md`,
  and anything that cannot be classified safely. After `y` you must also type `confirm`.
- Never allowed, no prompt: paths outside the vault, and writes to `.git/`, `.claude/`,
  `.mcp.json`, `.envrc`, `.vscode/` and similar configuration paths.

No answer within `PA_APPROVAL_TIMEOUT_S`, a closed terminal or any internal error means **deny**.

Every reply shows which tier answered it:

- `agent[1]>`: `help`, `status` and `sessions` (your [Claude Code sessions](#connect-your-claude-code-sessions)),
  `quota`, `quota reset`, `note <text>`, the task commands (`task <text>`, `tasks`, `task show <id>`,
  `task cancel <id>`) and `domains` / `domain <name>` are answered without any model.
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
| `PA_TASKS` | `on` | background tasks in the chat (`on`/`off`) |
| `PA_TASK_PARALLEL` | `2` | helpers running at once (1–2) |
| `PA_TASK_MODEL` | the chat's model | model alias for helpers |
| `PA_TASK_TIMEOUT_S` | `900` | a helper running longer is stopped (60–3600) |
| `PA_TASK_QUOTA_STOP` | `min(0.8, PA_QUOTA_STOP)` | helpers stop starting at this share of a usage window, leaving headroom for the chat |
| `PA_TASK_MAX_AGE_H` | `24` | queued tasks older than this expire instead of starting (1–720) |
| `PA_TASK_DISCORD` | `off` | `counts`: post task counts (never content) to `PA_DISCORD_WEBHOOK` (see [Notifications](#notifications)) |
| `PA_TASK_DELEGATION` | `off` | `on`: the main assistant may queue tasks itself, each with your approval |
| `PA_DOMAIN_CONTEXT_CHARS` | `4000` | character budget for domain notes attached to a helper's message (0–8000, `0` attaches none) |
| `PA_SESSIONS_SOURCE` | `hooks` | where `sessions` looks: `hooks` (status files), `agents` (`claude agents --json`) or `both`; see [below](#connect-your-claude-code-sessions) |

### Background tasks

Hand work to the assistant and keep chatting:

```
you> task @research summarise the backup options in wiki/
agent[1]> task 3f2a9c queued (0 running, 0 ahead) · result: outputs/tasks/3f2a9c.md
...
[task 3f2a9c done: outputs/tasks/3f2a9c.md]
```

| Command | What it does |
|---|---|
| `task [@domain] <text>` | queue a task (also `aufgabe`); `task add <text>` works too |
| `tasks` | open tasks, then the five newest finished ones |
| `task show <id>` | details and a preview of the result (marked as data written by a helper) |
| `task cancel <id>` | a queued task is cancelled at once, a running helper is stopped |

- Each task runs in a separate **helper**: a one-shot, locked-down Claude Code session in the vault with
  the same hardening as the chat, its own approval scope and no transcript. It can read the vault; with
  a `@domain` it may also append up to three lines `- YYYY-MM-DD (task <id>): finding` under `## Notes`
  in `wiki/domains/<domain>.md` (only at the end of the file, never inside the `Links:` line or an
  earlier note), and only after you approve. Every other write is denied without asking.
  Helpers have no Skill, Task or Agent tool and cannot start other helpers.
- At most two helpers run at once; the rest wait (`queued`, or `waiting` with the reason, e.g. the
  quota). Tasks run only while a chat is open, because approvals need you; a second open chat can add,
  list and cancel, but the tasks run (and ask) in the first one. When the first chat closes, the second
  one takes over and says so.
- The helper's final answer becomes `outputs/tasks/<id>.md`, written by the agent itself. The result is
  never fed back to the main assistant; you decide what to do with it. The main assistant may not write
  task results, and a turn in which it read them marks its approval prompts like note context.
- Limits: `PA_TASK_TIMEOUT_S` per task, at most 30 model turns per helper, at most 3 approval prompts per
  task (a helper prompt waits at most 2 minutes and steps aside when the chat needs the terminal), 20 open
  tasks, 30 task starts per day. Without fresh quota data only one helper runs, as a probe.
- A safety stop in any session (hook event, unexpected tool or plugin, output outside a turn) stops the
  chat **and** every helper until you restart.
- While you are idle at `you>`, a helper's approval prompt takes over the terminal; anything you were
  typing is discarded, and the prompt says so. In this mode the terminal has no line history.
- With `PA_TASK_DELEGATION=on` the main assistant gets a tool to queue a task (up to 300 characters). Each
  delegation needs your approval (level 3 with `confirm` when the turn carries note or skill context),
  at most one per turn; the assistant only gets the task id back, never the result. Delegated tasks are
  marked `[delegated]`.

### Domains

A domain is a memory file per topic for background tasks: `wiki/domains/<domain>.md`. The first
`task @<domain> …` creates it (names: lowercase letters, digits and dashes, at most 32 characters; at most
50 domains):

```markdown
# Domain: research

Links: finance, home-network

## Notes
- 2026-10-18 (task 3f2a9c): restic supports append-only repositories
```

| Command | What it does |
|---|---|
| `domains` | one line per domain: links, number of notes, last change (also `bereiche`) |
| `domain <name>` | the links and the newest 20 note lines (also `domain show <name>`, `bereich <name>`) |

- A helper for a `@domain` task gets the domain's notes, then the notes of the domains on its `Links:`
  line, in front of its message: inside a `<domain-notes>` block with a random id, escaped and labelled as
  reference data, never instructions. Own notes first (up to 60 % of `PA_DOMAIN_CONTEXT_CHARS` when linked
  notes come too), the newest lines kept. Links go one level deep (a linked domain's links are not
  followed) and at most five count; only you edit them.
- Helpers add notes only through the approved append described above. Domain notes are never attached to
  chat messages or the morning brief. Symlinked or oversized (over 128 KiB) domain files are not attached.

### The vault

The assistant works in a Markdown vault. `personal-agent vault init` creates the skeleton (`chat` does
it too on start); existing files are never overwritten, and symlinks are left untouched:

```
CLAUDE.md               structure rules, so the vault does not sprawl
heute.md                today's focus: Top 3 and notes
raw/                    captured, unprocessed notes: raw/inbox-YYYY-MM-DD.md
wiki/                   organized articles, one topic per file, listed in wiki/_master-index.md
outputs/                generated results such as briefs; outputs/tasks/<id>.md are task results
wiki/domains/           domain notes for tasks, created on first `task @<domain>`; never attached to
                        chat messages or the brief
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

Not at the terminal? `personal-agent chat --remote-approvals` sends each approval to your phone instead,
through the running `personal-agent serve`:

- With a paired [Telegram bot](#telegram) (the default when it is ready): a message with the tool name,
  the level, where the request comes from and **Approve** / **Deny** buttons. Level 3 also needs your
  approval PIN as the next message. With `PA_PUBLIC_APPROVAL_URL` set, the message also carries a review
  link to the one-time page, where you can see the request before you decide.
- Otherwise, with Discord: a short notice with the tool name, the level and a one-time link. The link
  opens a small page served by `personal-agent serve`, where you allow or deny.

Neither ever contains the tool input. `PA_NOTIFY_APPROVAL=telegram|discord|none` picks the path
explicitly. The Discord setup:

1. Create a Discord webhook for a private channel and set `PA_DISCORD_WEBHOOK` (treat it as a secret).
2. Set `PA_PUBLIC_APPROVAL_URL` to the address your phone uses to reach the server, for example your
   tailnet name. Forward **only** `/approve/` (with the trailing slash) to `127.0.0.1:8787`. Never
   forward `/internal/` or `/escalate`; prefer tailnet-only access over a public URL.
3. Set `PA_APPROVAL_PIN` (at least 6 digits). Level 3 needs the typed `confirm` **and** this PIN on the
   page; three wrong PINs deny the request.
4. Run `personal-agent serve` (same `PA_API_TOKEN`), then `personal-agent chat --remote-approvals`.

Each link and each button works once and expires shortly before `PA_APPROVAL_TIMEOUT_S`. Expired,
cancelled or unanswered requests are denied. At most five requests may be pending and at most five
notices or prompts are sent per ten minutes (console approvals and Telegram turns together).

| Variable | Default | Meaning |
|---|---|---|
| `PA_DISCORD_WEBHOOK` | – | Discord webhook URL (`https://discord.com/api/webhooks/...`), never logged |
| `PA_PUBLIC_APPROVAL_URL` | – | base URL of the approval page, `https://` is added if missing |
| `PA_APPROVAL_PIN` | – | PIN for level-3 approvals on the page and over Telegram, required when remote approvals or `PA_TELEGRAM_TOKEN` are set; choose one that differs from `PA_CALL_PIN` |
| `PA_TRUSTED_CLIENTS` | – | extra private networks allowed on `/escalate` and `/internal/`, e.g. `172.17.0.1/32` when `serve` runs in a container (host clients then arrive from the bridge gateway and otherwise get 404) |

### Morning brief

`personal-agent brief` builds today's brief **without any model**: the date, the focus from `heute.md`,
open `- [ ]` tasks from `heute.md` and `wiki/` (at most 15; `raw/` is unprocessed capture and stays
out), a summary of your Claude Code session status, the quota status and the number of new captures
since the last brief. It prints the brief and writes `outputs/brief-YYYY-MM-DD.md` in the vault.

```bash
personal-agent brief             # print and write the file
personal-agent brief --send      # also send it to the brief channels (paired Telegram bot, else Discord)
personal-agent brief --polish    # let the small model rewrite the text (see below)
personal-agent brief --quiet     # write only
```

With `PA_BRIEF_TIME=07:30`, `personal-agent serve` sends the brief once a day at that local time
(`PA_TELEGRAM_TOKEN` with a paired bot, or `PA_DISCORD_WEBHOOK`, is enough; remote approvals are not needed). The day is recorded in
`$PA_STATE_DIR/brief.json`, so a restart, a crash or an earlier manual `brief --send` never causes a
second post. A failed post is retried at most three times, at least five minutes apart. After downtime
the brief is only sent while the grace window (`PA_BRIEF_GRACE_MIN`) is still open; missed days are
never caught up. Local time comes from the process: set `TZ` (e.g. `TZ=Europe/Berlin`) in a container.

**Privacy:** on Telegram the brief is sent as full plain text by default (`PA_BRIEF_TELEGRAM=full`, links
defanged); it then passes through Telegram's servers (see [Telegram](#telegram)). Claude Code sessions
appear there only as state counts: project names and waiting texts stay on your machine, as with
`status` and `sessions` over Telegram. The polish step never sees them either. By default Discord only gets counts (`PA_BRIEF_DISCORD=counts`): number of open tasks and
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
effect when a channel shows the full text (`PA_BRIEF_DISCORD=full` or `PA_BRIEF_TELEGRAM=full`); in
`counts` mode it would spend a model call every day on text nobody sees, and is skipped.

If a post fails halfway, the exact chunks of that attempt are kept per channel in
`$PA_STATE_DIR/brief-chunks.json` (0600, removed after a successful send) and a retry posts only the
chunks that did not go out, and only to the channels that did not get everything, even when the quota or
session status changed in between.

| Variable | Default | Meaning |
|---|---|---|
| `PA_BRIEF_TIME` | – | daily brief time `HH:MM` (local), empty = off; needs `PA_TELEGRAM_TOKEN` or `PA_DISCORD_WEBHOOK` |
| `PA_BRIEF_GRACE_MIN` | `120` | minutes after `PA_BRIEF_TIME` in which a late or retried send is still allowed (1–720) |
| `PA_BRIEF_POLISH` | off | `1`/`true`/`yes`: polish the scheduled brief with the small model (only with `PA_BRIEF_DISCORD=full`) |
| `PA_BRIEF_DISCORD` | `counts` | `counts` sends only counts to Discord, `full` the escaped brief text |
| `PA_BRIEF_TELEGRAM` | `full` | what the brief shows on Telegram: `full` (plain text, links defanged) or `counts` |

### Notifications

All notices go through one notification layer. There are five kinds: `brief` (the morning brief),
`alert` (an escalation sent to `serve`, and whether it calls you), `approval` (the remote approval
notice), `task_done` (background task counts) and `info` (fixed operational notices, e.g. "the morning
brief gave up for today"). `PA_NOTIFY_<KIND>` picks the channels for a kind: a comma list of `telegram`,
`discord`, or `none`. A channel named explicitly that is not ready is skipped with one log line; nothing
silently falls back to another channel.

Without `PA_NOTIFY_<KIND>`, a kind goes to Telegram when a paired Telegram bot is ready, and otherwise:

| Kind | Default without Telegram |
|---|---|
| `brief` | Discord when `PA_DISCORD_WEBHOOK` is set |
| `approval` | Discord when `PA_DISCORD_WEBHOOK` and `PA_PUBLIC_APPROVAL_URL` are set |
| `task_done` | Discord only with `PA_TASK_DISCORD=counts` |
| `alert`, `info` | nowhere (opt-in on Discord with `PA_NOTIFY_ALERT=discord`, `PA_NOTIFY_INFO=discord`) |

So nothing changes for an existing Discord setup. Telegram is ready once a bot is [paired](#telegram);
until then `telegram` routes are skipped with a log line. `personal-agent telegram status` prints the
resolved channel for each kind.

**Privacy per kind.** An approval notice never contains the tool input: only the tool name, the level,
where the request comes from and the one-time link. Task notices carry counts only; with
`PA_NOTIFY_TASK_DETAIL=title` they add the task id, its status and the cleaned title (at most 60
characters), never results, errors, domains or paths. Info notices are fixed texts. Every text from notes,
status files or events is made inert for the channel (no markup, no mentions, links shown as `https[:]//`).
Each channel has its own size limit (Discord 1900 characters, Telegram 4000 UTF-16 units per message);
longer notices are split at line boundaries into at most five messages and cut with a note. Alerts are
limited to ten per ten minutes and the same event key to one per hour; info notices likewise.

| Variable | Default | Meaning |
|---|---|---|
| `PA_NOTIFY_BRIEF`, `PA_NOTIFY_ALERT`, `PA_NOTIFY_APPROVAL`, `PA_NOTIFY_TASK_DONE`, `PA_NOTIFY_INFO` | default rule above | `telegram`, `discord`, a comma list of both, or `none`; `PA_NOTIFY_APPROVAL` takes one value |
| `PA_NOTIFY_TASK_DETAIL` | `counts` | `title` adds task id, status and cleaned title to task notices |
| `PA_TELEGRAM_TOKEN` | – | Telegram bot token (a secret, never logged or shown); needed by any `telegram` route |

### Telegram

A Telegram bot brings the agent to your phone: notices, approvals with buttons, and a chat with the same
assistant as `personal-agent chat`. `serve` reaches Telegram by long polling over outbound HTTPS only:
no webhook, no open port.

**Privacy first: bot chats are not end-to-end encrypted.** Everything you exchange with the bot passes
through Telegram's servers: your messages and the assistant's replies, the full morning brief (by
default), alert summaries, task notices, the tool names of approvals and, when set, the review link.
Discord keeps counts only by default. If that is not acceptable for some content, keep it in the
terminal.

Set it up:

1. In Telegram, open the official **@BotFather** chat, send `/newbot` and follow the steps. Copy the
   token it shows and keep it in your password manager.
2. In the bot's settings (BotFather → your bot → Bot Settings → Allow Groups), turn **groups off**, so
   the bot never gets updates from a group.
3. Set `PA_TELEGRAM_TOKEN` and `PA_APPROVAL_PIN` (at least 6 digits, different from `PA_CALL_PIN`)
   in your `.env`. `serve` refuses to start with a token but without the PIN.
4. Pair the bot with your account in a terminal (a running `serve` does not poll while the bot is
   unpaired; to pair a new account, `unpair` first):

   ```bash
   personal-agent telegram pair
   ```

   It prints a one-time link `https://t.me/<bot>?start=<code>` (192 random bits, single use, valid for
   10 minutes, stored only as a hash). Open it on your phone and press Start. The terminal then shows
   the requesting user id and asks `bind this account? [y/N]`. Only after `y` is that private chat
   bound, and the bot confirms. `--yes` binds without asking (for a terminal without input), and when
   stdout is not a terminal the code is not printed unless you pass `--print-code`, so it does not end
   up in a log.
5. Start (or keep running) `personal-agent serve`. It starts polling within five seconds of a pairing
   and stops within five seconds of an unpairing.

```bash
personal-agent telegram status     # token set, bot, pairing (chat id masked), poller, webhook, routes
personal-agent telegram unpair     # forget the chat; a running serve stops polling
```

The token is never printed, logged or passed to Claude Code; log lines are filtered for anything that
looks like a bot token. If you think the token leaked: `personal-agent telegram unpair`, revoke the
token in BotFather (`/revoke`), set the new `PA_TELEGRAM_TOKEN`, restart `serve`, pair again.

**What the bot accepts.** Only messages from the paired user in its private chat count; everything else
(other users, groups, channels, other bots) is ignored without any reply. Forwarded messages and
messages sent through an inline bot are refused, messages older than five minutes (sent while `serve`
was offline) are not executed, and at most 20 messages per minute are read.

**Chat.** Text from your chat goes through the same router as the terminal chat: tier-1 commands
(`help`, `note <text>`, `tasks`, `task <text>`, `task show <id>`, `quota`, `domains`), the small model
and the main assistant, with the same approvals, quota guard, slash-command refusal and safety
tripwires. One turn runs at a time; up to three messages wait in a queue, more get "busy". `/new`
starts a new conversation. Differences from the terminal, because a stolen phone or account must not
get more than the terminal gives:

- `status` and `sessions` answer "only in the terminal": session names and waiting texts stay local.
- `quota reset` is refused; the main assistant cannot delegate.
- `task <text>` only **queues** a task (`PA_TELEGRAM_TASKS=off` turns that off). It runs when a
  terminal chat is open, with that chat's approvals, which say `task 3f2a9c · telegram`, and the chat
  prints where it came from. Notes from Telegram are marked `[via telegram]` in the vault.
- At most `PA_TELEGRAM_MAX_TURNS_PER_HOUR` model turns per hour (tier 1 stays free).
- A safety stop (a hook or an unexpected plugin in a session) stops the terminal chat and the Telegram
  assistant alike. To lift it, run `personal-agent safety clear` in a terminal, then restart `serve` and
  any open chat: a running process keeps a stop it already holds.
- `/lock` in the chat removes the pairing at once, also while a PIN is awaited: pending approvals are
  denied, queued messages dropped and a running turn stopped (you can pair again in a terminal).
- `PA_TELEGRAM_INBOUND=off` turns the chat off and keeps notices and approvals.

**Approvals over Telegram.** Turns that start in Telegram always ask in Telegram. Each request shows a
number, the level, the tool name and where it comes from, with **Approve**, **Deny** and, for Telegram
turns, **Deny and stop turn**. Only presses of the paired user on that very message count, each button
works once, and the buttons disappear after the decision. Level 3 needs your approval PIN as the next
message within two minutes; the PIN message is deleted (best effort), never reaches the assistant or a
log, and three wrong PINs deny. Every wrong PIN counts, from Telegram and from the page alike: six
within 24 hours disable level-3 remote approvals for 24 hours (`personal-agent telegram status
--clear-lock` resets that, also for a running `serve`). A message that
equals one of your PINs is never sent to the assistant, whatever the state. Over Telegram, level 2 is
one tap: it confirms intent, it is not a security boundary against someone holding your unlocked phone.

| Variable | Default | Meaning |
|---|---|---|
| `PA_TELEGRAM_TOKEN` | – | the bot token from BotFather (a secret) |
| `PA_TELEGRAM_INBOUND` | `on` | `off`: no chat over Telegram; notices and approvals still work |
| `PA_TELEGRAM_TASKS` | `queue` | `off`: `task <text>` from Telegram is refused |
| `PA_TELEGRAM_MAX_TURNS_PER_HOUR` | `30` | model turns from Telegram per hour (1–600) |
| `PA_BRIEF_TELEGRAM` | `full` | what the brief shows on Telegram: `full` or `counts` |

There is deliberately no setting for another API address: a test server address left in a real `.env`
would send the token in clear text to whatever local process holds that port.

### Mail

The assistant reads your mailboxes, sorts them and warns you; **it never sends, deletes, moves or marks
anything as read**. Passwords live only in a separate connector file (`PA_MAIL_CONFIG`, mode 0600, outside
the vault); only a short-lived worker process (`python -I -S`, minimal environment) ever opens it. IMAP runs
over implicit TLS only, with `EXAMINE` and `BODY.PEEK` so nothing changes on the server.

```json
{"version": 1,
 "accounts": [{"name": "work", "host": "imap.example.org", "port": 993, "user": "me@example.org",
               "password": "<app password>", "folders": ["INBOX"],
               "address": "me@example.org", "drafts_folder": "Drafts", "allow_draft_append": true}]}
```

| Command | What it does |
|---|---|
| `mail` | counts per folder, mails per category (deadline/legal, invoice/payment, complaint, request, confidential, newsletter), the latest mails, and mailbox problems |
| `mail refresh` | checks the server now (rate-limited) |
| `mail show <id>` | one mail as plain text, marked as data, with warning signals and a **payment check**: every IBAN is shown masked and compared with the bank accounts this sender used before (new or changed account = check by phone before paying) |
| `mail trust <id>` | confirm the bank account in this mail after you checked it |
| `mail draft <id>` | puts the reply in `outputs/drafts/<id>.md` (the assistant writes it there, with your approval) into the account's drafts folder; you send it yourself from your mail program |

Sorting uses fixed rules, not a model, so a mail can never talk its way into another category. Mailbox
health shows up in `mail` and in the morning brief: a failed login (often a changed app password), a check
that is hours old, or several working days without any new mail. A dead login must never look like a quiet
inbox. Drafts need `"allow_draft_append": true` for the account; that worker session may additionally use
`LIST` and `APPEND` (with the `\Draft` flag), nothing else.

## Connect your Claude Code sessions

Your sessions report their state through a small hook (standard library only) that writes one status
file per session. Print the settings snippet for it:

```bash
personal-agent hooks snippet
```

It prints the value for the `"hooks"` section of your Claude Code settings (for example
`~/.claude/settings.json`) with the absolute paths of your interpreter and the packaged hook, and
`PA_STATUS_DIR=...` in front when your status directory is not the default. Merge it by hand: the
command **never opens, reads or edits any settings file**. Example output (shortened):

```json
{
  "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "/usr/bin/python3 -I -S /.../personal_agent/claude_status_hook.py" }] }],
  "PreToolUse":       [{ "hooks": [{ "type": "command", "command": "..." }] }],
  "Notification":     [{ "hooks": [{ "type": "command", "command": "..." }] }],
  "Stop":             [{ "hooks": [{ "type": "command", "command": "..." }] }],
  "StopFailure":      [{ "hooks": [{ "type": "command", "command": "..." }] }],
  "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "..." }] }]
}
```

In the chat, `sessions` (also `sitzungen`) lists them without a model, waiting ones first:

```
you> sessions
agent[1]> 3 sessions (1 waiting)
waiting     4 min  shop-backend                     Claude needs your permission to use Bash
working    12 min  docs-site
idle       40 min  personal-agent
```

- Only the project folder's name is shown, never the full path, and all text is cleaned. `status`
  keeps its older output (errors first), which the phone call uses too.
- `PA_SESSIONS_SOURCE=agents` or `both` also asks Claude Code itself through the documented
  `claude agents --json` (merged by session id; Claude Code's state wins). It is **off by default**:
  agent view is a research preview, the listing covers every project, and the docs do not say whether
  the call starts the agent-view supervisor. It runs only for the typed `sessions` command, with a
  minimal environment (no API keys, never the assistant's own config dir), a 5 s timeout and a 10 s
  cache; nothing from it is logged or sent to Discord, Telegram or the phone.
- The agent **never sends anything into your sessions**: Claude Code documents no stable way for a
  separate process to do that. To answer a waiting session, attach in a terminal, use agent view or
  Remote Control. Background in [docs/SESSION-CONTROL.md](docs/SESSION-CONTROL.md).

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
