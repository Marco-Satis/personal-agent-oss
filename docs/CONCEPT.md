# Concept

Working title: **personal-agent** (the final name is still open).

## Goal

A self-hosted personal agent built on **Claude Code** that

1. **calls you on the phone** when something important happens, and lets you ask what is going on,
2. **watches and later coordinates several Claude Code sessions** on your behalf,
3. **reads e-mail and drafts replies** for you to approve,
4. optionally offers a **voice-controlled dashboard** (status, numbers, news).

Normal notifications stay in whatever chat channel you already use. The phone rings only when it matters.

## Principles

- **Small and auditable.** v1 has zero runtime dependencies (Python standard library only).
  Fewer packages means less supply chain risk for a tool that will touch mail and sessions.
- **Built on Claude Code itself**, not on a second agent framework. Observing sessions uses what Claude
  Code documents (hooks, `claude agents --json`); sending into running sessions is not implemented
  (see [SESSION-CONTROL.md](SESSION-CONTROL.md)).
- **Provider-neutral.** Twilio is the first phone provider because its webhook model is simple. The
  conversation logic does not know which provider is used.
- **Safe by default.** The default provider places no call. Every dangerous capability is off until
  configured, and even then limited.

## Architecture

```
 Claude Code sessions                       personal-agent (127.0.0.1)
 ┌─────────────────────┐   status files    ┌──────────────────────────────────┐
 │ hooks/claude_status │ ────────────────▶ │ status reader (read-only)        │
 │ _hook.py            │                   │        │                         │
 └─────────────────────┘                   │        ▼                         │
                                           │ session watch ──┐                │
 cron / n8n / scripts                      │                 ▼                │
 ┌─────────────────────┐  POST /escalate   │ escalation policy (limits)       │
 │ personal-agent      │ ────────────────▶ │        │                         │
 │ escalate ...        │  (bearer token)   │        ▼                         │
 └─────────────────────┘                   │ call provider (fake / Twilio)    │
                                           │        │                         │
                                           │ conversation state machine       │
                                           └────────┼─────────────────────────┘
                                                    │ /twilio/* only, via reverse proxy
                                                    ▼
                                              phone network ──▶ your phone
```

| Module | Job |
|---|---|
| `status.py` | Reads session status files, cleans every text field; the chat's read-only `sessions` view (waiting first), optionally merged with `claude agents --json` |
| `claude_status_hook.py` | The status hook, shipped inside the package (byte-identical to `hooks/claude_status_hook.py`) so `personal-agent hooks snippet` can point at it |
| `escalation.py` | Decides whether an event may call: severity, de-duplication, interval, daily cap, quiet hours, acknowledgements |
| `conversation.py` | The call as a deterministic state machine: PIN, alert, menu |
| `telephony.py` | Fake provider, Twilio REST call, TwiML rendering, webhook signature check |
| `server.py` | Local HTTP server, session watch thread, morning brief thread |
| `bridge.py` | One locked-down Claude Code process per conversation (stream-json) |
| `approval.py` | Approval levels, the broker behind a private Unix socket, the terminal prompt |
| `approval_server.py` | The permission tool Claude Code calls (stdio MCP server, standard library only) |
| `quota.py` | Quota guard: refuses a turn before any model call when the subscription window is nearly used up or paid usage shows up |
| `router.py` | Three tiers: fixed answers, a small model without tools, the main locked-down session; `note` capture, slash-command allowlist, note excerpts |
| `vault.py` | The Markdown vault (the assistant's workspace): skeleton, note capture, relevant-note selection, safe fd-based file access |
| `plugins.py` | Plugin checker (read-only, fd-based), enable/disable by checked copy plus digest registry, loading per chat start |
| `brief.py` | Morning brief: deterministic content, Discord rendering and chunking, optional polish by the small model, once-per-day state and scheduler |
| `tasks.py` | The task board (state_dir/tasks.json, atomic, flock, compare-and-set transitions), tier-1 task commands, helper prompt and result file rendering |
| `coordinator.py` | Runs tasks in locked-down helper sessions (at most two), watchdog, cancel, quota and latch handling; the task facade for the router and delegation; counts-only Discord notices |
| `domains.py` | Domain memory files (`wiki/domains/<domain>.md`): links (depth 1, at most five), skeleton creation, budgeted notes for helper prompts, tier-1 `domains`/`domain <name>` |
| `console.py` | The chat terminal: one owner of stdin for `you>` and approval prompts, non-blocking notices |
| `textsafe.py` | One definition of invisible and control characters for all untrusted text |
| `remote.py` | Remote approvals: Discord webhook notice, one-time approval page, the chat-side channel |
| `cli.py` | `serve`, `escalate`, `status`, `simulate`, `chat`, `vault`, `plugins`, `brief`, `hooks snippet` (prints only) |
| `plugins/core/` (repo folder, not a module) | The shipped core plugin: skills `organize-inbox`, `daily-plan` and `capture-note` for the vault; checked like any other plugin |

## v1 scope (this release)

- **Escalation call:** an event with severity `critical` (from a script, cron, n8n, or the built-in
  session watch) triggers an outbound call, within the configured limits.
- **Session status by phone:** after the PIN, you hear the alert and can ask for the status of your
  Claude Code sessions (waiting / working / idle / error, how long, short summary).
- **Acknowledge:** pressing 3 marks the alert as handled; it will not call again about it for 24 h.
- **Simulator:** the full conversation runs in the terminal without any provider or cost.

Explicitly **not** in v1: sending anything into a session, reading mail, inbound calls, an LLM in the
call loop. These come later, behind the security model below.

## Security model

An agent that reads other people's mail, answers customers and controls coding sessions is a prime
prompt injection target: a crafted e-mail could try to become a command in your session. The design
starts from that threat.

### 1. Data never becomes a command

Text from sessions, e-mails, web pages or callers is **data**. It may be shown, read aloud or
summarised. It never chooses an action. In v1 the phone menu is a fixed set of commands (digits or a
few keywords); no text from a status file or event can add a command. Status and event text is
stripped of markup and control characters and cut to a short length before it is spoken.

### 2. Separate roles, separate rights

Later versions split the agent into processes with different rights:

| Role | May read | May write / act |
|---|---|---|
| Watcher (v1) | session status files | place a call |
| Mail reader (v3) | mailbox, read-only credentials | create **drafts** only |
| Session controller | session list | **not implemented**, see [SESSION-CONTROL.md](SESSION-CONTROL.md) (no documented, stable mechanism) |

The mail reader cannot reach the session controller's interface, and the session controller never
receives raw mail text, only a short structured summary that you confirmed.

### 3. Approval before anything leaves

Nothing is sent on your behalf without an explicit approval: no e-mail, no customer reply, no message
into a session. Drafts are fine; sending is a separate, confirmed step.

**How the assistant asks (chat).** Claude Code runs with `--permission-prompt-tool mcp__pa__approve`.
That tool is a tiny MCP server (`approval_server.py`), started as `python -I -S <absolute path>` so that
nothing in the assistant's workspace and no `.pth` file can replace it. It forwards every request over a Unix socket
(directory 0700, socket 0600, random per-session token, peer uid check) to the broker in the agent
process, which decides:

| Level | Examples | Decision |
|---|---|---|
| 1 read | Read, Glob, Grep inside the workspace | allowed without asking |
| 2 write | Write, Edit, NotebookEdit; MCP tools whose verb is a known read or write verb | you answer y/N |
| 3 danger | names with send, delete, pay, publish, post, share, run, ...; `CLAUDE.md`, `AGENTS.md`, `.github/`; anything unknown | y/N **and** you type `confirm` |

Before any level applies, the broker denies without asking:

- requests outside an active chat turn, or for a tool call the assistant did not announce in the
  stream (id, name and input hash must match; each call is reviewed once),
- any path outside the workspace (symlinks are resolved, `~` is expanded),
- writes to `.git/`, `.claude/`, `.mcp.json`, `.envrc`, `.vscode/`, `.idea/`, `.claude.json`, which
  would grant code execution or change the assistant's own configuration.

Prompts are shown one at a time, the main session's first (see 3d for helpers). At most three
requests per session and six in total may queue; after two refusals in one turn the rest of that
turn is denied. The prompt renders values as ASCII-escaped JSON, so control, bidi
and look-alike characters are visible, and every cut is marked; a level-3 prompt shows the complete
input when the short form would cut it. Typed-ahead input is discarded before a prompt, and a
non-interactive stdin never approves. **Every failure is a deny**: no channel, timeout, a closed
terminal, a wrong token, an unreachable socket. The tool input is never modified.

**Approving from the phone.** With `chat --remote-approvals` the broker asks through the running
`serve` process instead of the terminal, over the bearer-protected local `/internal/approvals` API.
`serve` posts a notice to a Discord webhook (write-only; no bot token, nothing is read back) with
only the tool name, the level and a one-time link `/approve/<random id>`. Mentions and embeds are
switched off, and the link is wrapped so no preview crawler fetches it. The page shows the request
and accepts a POST only with the per-request CSRF token, a matching `Origin` (when sent) and a
same-origin `Sec-Fetch-Site` (when sent); GET never changes anything. Level 3 needs the typed
`confirm` and `PA_APPROVAL_PIN`; three wrong PINs deny. A link works once, expires 5 seconds before
the agent's own deadline, and is cancelled when the agent stops waiting, so a late click never shows
"approved" for an action that was denied. Level-3 inputs too long to show in full are denied rather
than sent to the page. At most five requests are pending and at most five notices go out per ten
minutes. Logs carry only the first six characters of an id, never the webhook URL, the CSRF token, the
PIN or the input.

### 3a. Vault and notes

The chat's working directory is the vault (`raw/` captures, `wiki/` articles, `heute.md`, `outputs/`).
Notes hold captured mail and web text, so they are treated as hostile data:

- **Excerpts are delimited and escaped reference data.** Relevant paragraphs from `wiki/` and
  `heute.md` (never `raw/` or `outputs/`) are picked with a small token, file-name and recency score
  within a character budget, and placed before the message in `<vault-excerpts id="<random per turn>">`.
  Text and paths are escaped (`& < > " '`), so a note cannot close the block, open a new one or forge
  attributes; the person's request follows in `<user-message>`. A system prompt file (0600, in the
  broker's runtime directory outside the vault) states the layout and that excerpts and file contents
  are data, never instructions. Auto-loading of the vault's `CLAUDE.md` under `--restricted` is not
  relied on.
- **Approval context.** When a turn carries excerpts or used a plugin skill, every approval prompt in it
  says that the request may come from note text or a plugin skill. A typed `/<plugin>:<name>` command is
  expanded by Claude Code itself, without a Skill tool call, so the router marks such a turn before it
  is sent.
- **Slash-command allowlist.** Claude Code expands any message that starts with `/` (built-in commands
  such as `/config` or `/clear` included). The final body of every turn, after `!` and after leading
  whitespace and invisible characters, may start with `/` only when its first word is an allowlisted
  `/<plugin>:<name>`; everything else is refused without starting a session.
- **Capture without a model.** `note <text>` appends one line to `raw/inbox-YYYY-MM-DD.md`. Control
  and invisible characters (bidi, zero-width, Unicode TAG characters, variation selectors, private use)
  are removed, and the text cannot fake a task (`[ ]`) or a processed marker.
- **File-system rules.** Sync tools can place symlinks and FIFOs in the vault. Reads walk directory
  descriptors, skip symlinks, hidden and non-regular entries, open with `O_NOFOLLOW | O_NONBLOCK`, check
  the descriptor with `fstat`, and are capped (512 KiB per file, 2000 files, 8 MiB per scan). Writes walk
  fd-relative with `O_NOFOLLOW` on every component; outputs are written to a temporary file and renamed
  within the same directory descriptor.
- **Workdir guard.** The vault must not contain the package, interpreter, Claude Code binary or config,
  state, status, plugins or runtime directories or state files, and must not lie inside the status,
  config, plugins or runtime directory.

### 3b. Plugins

Extensions are standard Claude Code plugins. Their skill, command and agent text goes into the model
context, and some plugin features run code, so every plugin is checked before it is enabled and again on
every chat start. Evidence from a test with Claude Code 2.1.302 (real CLI, minimal environment, the
bridge's flags plus `--plugin-dir`):

- Plugin **hooks ran arbitrary shell commands** despite `--restricted` and `--strict-mcp-config`, before
  the init event, in the session's working directory and with its environment (an OAuth token when one is
  passed) and config dir. `--settings '{"disableAllHooks":true}'` stopped them and kept skills working;
  `--bare` also stops them but breaks the subscription login.
- A plugin's **MCP server started** as soon as `--strict-mcp-config` was missing, together with the
  account's connector servers, whose tools appeared although `--tools` did not name them. `--tools`
  filters only built-in tools.
- The **Skill tool asked nobody** by default and the init event listed built-in skills such as
  `update-config` and `schedule`. With `"permissions":{"ask":["Skill"]}` every Skill call went through
  `--permission-prompt-tool` with input `{"skill": "<name>"}`.
- A message starting with `/` was expanded by the CLI itself, even without the Skill tool.

Hence:

- **Checker (the boundary).** Refused without any override: hooks (folder, manifest key, frontmatter
  key), MCP and language servers, `allowed-tools`/`model`/`tools`/`permissionMode`, unknown manifest or
  frontmatter keys, YAML that a small parser could read differently from the CLI's real parser (BOM,
  `\r`, tabs, duplicate or quoted keys, flow, block, anchor, tag and merge syntax, comments), symlinks,
  hard links, special files, executable bits, binaries and shebangs, hidden entries and odd names,
  supporting files in skill folders (the plugin folder is outside the vault, so a skill must be one
  self-contained `SKILL.md`), and caps on depth, files, sizes and components. Content scans run on
  NFKC-normalised, casefolded lines: credentials (reported by line, never echoed), URLs and network tools
  in prompt text, encoded blobs, `eval`/`exec`, shell expansion (``!` ``, fenced `!` blocks, `$(`, `${`),
  `@file` references (quoted forms such as `@"~/x"` included) and invisible characters. Descriptions sit in the context on every turn, so an
  override phrase there is an error and all loaded plugins together get 4 KiB of descriptions. The checker
  never runs plugin code, walks directory descriptors without following symlinks and opens files
  non-blocking with an `fstat` check.
- **Checked copy plus digest.** `enable` writes exactly the bytes it checked into a staging folder (files
  0644, folders 0755, no metadata), checks the copy again, compares digests and renames it into
  `state_dir/plugins/<name>`. The registry (`plugins.json`, outside the plugins folder) holds the digest;
  a plugin edited afterwards is skipped at the next start. State and plugins folders must be real
  directories owned by the user and not writable by group or others. `core` and `cc-*` are reserved.
- **Session hardening.** Every session (main, small and helpers) runs with `--strict-mcp-config` and
  `--settings` holding `disableAllHooks`, `crossSessionInbound: refuse`, an `ask` rule for Skill and the
  delegation tool, and deny rules for SendMessage, ListAgents, Task and Agent (see 3d). The broker allows a Skill
  call only for `<plugin>:<name>` of a loaded, checked plugin and denies everything else without asking.
  One `--plugin-dir` per plugin, never a parent folder. The small model session gets no plugins.
- **Slash allowlist.** Only `/<plugin>:<name>` words enumerated by the checker pass; `!/config`,
  `/core` or an invisible character before `/` are refused before any session starts.
- **Stream tripwire (not a boundary).** The router aborts the turn, stops the process and refuses
  further turns until restart on any `hook*` system event, on an init event listing a tool outside the
  allowlist, an MCP server other than the approval server, a non-built-in plugin that was not passed (or
  from another path), a skill of an unknown plugin, or malformed lists, and on a Skill tool call outside
  the allowlist. It is only a tripwire: a UserPromptSubmit hook emits no event, and a SessionStart hook
  has already run when its event arrives.
- **Core plugin, no exceptions.** The shipped `plugins/core` (skills `organize-inbox`, `daily-plan`,
  `capture-note`) gets no special trust: it passes the same checker on every chat start (a test checks
  the real folder), has no hooks, MCP servers, commands or agents, and is loaded first with its own
  `--plugin-dir`. Its skills use only the vault file tools, so every write still goes through the
  approval broker; they tell the model to stay inside the vault layout, keep writes few, never touch
  `CLAUDE.md`, `outputs/`, `.git` or `.claude`, and treat note text as data. The name `core` is reserved
  so an enabled copy cannot shadow it.

### 3c. Morning brief

The brief pushes private and untrusted text (tasks and notes hold captured mail and web text) to an
outside service, and it runs unattended. Hence:

- **No model by default.** The brief is built deterministically from the vault, the status files and the
  quota state, and written to `outputs/brief-YYYY-MM-DD.md`. `raw/` tasks are left out.
- **Counts only to Discord by default** (`PA_BRIEF_DISCORD=counts`). With `full`, every vault- or
  status-derived string goes through the same escaper as the approval notices (`discord_safe`: no
  markup, no mentions) and URLs are defanged (`https[:]//`), so masked links and fake headings cannot
  render. Payloads keep `allowed_mentions: {"parse": []}` and suppress embeds; messages are split at line
  boundaries into at most five chunks of 1900 characters.
- **Polish is optional and contained.** The small model gets the escaped brief inside `<brief>` with no
  tools, no MCP, no plugins, no transcript (`--no-session-persistence`) and a fresh private working
  directory outside the vault, through the same stream tripwire as the chat. A fresh quota guard is read
  for every run and a blocked guard starts no process. Output that adds a URL, a domain, an `@` or a
  number not in the deterministic brief is rejected. At most one model call per day; the result is reused
  from `state_dir/brief-polished.json`, never from the vault copy (the assistant can write `outputs/`, so
  injected note text could plant a "polished" file there). The scheduler polishes only in `full` mode,
  where the text is actually shown.
- **At most once per day.** `brief.json` records the day, `sending`/`sent`/`failed`, attempts and the
  chunks already posted. Every run holds an flock on `brief.lock`; the scheduler takes it without waiting
  and decides on the state it re-reads under the lock. `sending` is written before the first post and a
  failure to write it means no post; a leftover `sending` after a crash counts as sent. Retries: at most
  three, five minutes apart, inside the grace window, resuming after the chunks already posted. The resume
  source is the stored chunk list of the failed attempt (`state_dir/brief-chunks.json`, checked against
  the digest in `brief.json`), not a fresh rendering: quota, session status and captures change between
  attempts, and a new rendering would repost chunks that already went out. In-memory
  counters per day keep a deleted, corrupt or unwritable state file from causing a post on every tick.
  Downtime past the grace window means no post that day and no catch-up. The state folder must be a real
  directory owned by the user and not writable by others.

### 3d. Tasks and helpers

The person hands work to the assistant in the chat (`task [@domain] <text>`); a helper does it in the
background and the result lands in the vault. Decisions:

- **Only inside a chat (D1).** Approvals need the person, so tasks run only while a `chat` is open,
  never in `serve`. Queued tasks wait for the next chat; tasks older than `PA_TASK_MAX_AGE_H` expire
  instead of starting a backlog unattended.
- **The board.** `state_dir/tasks.json` (never in the vault; the workdir guard lists it), validated field
  by field on every read, at most 1 MiB, 20 open and 200 kept tasks. Every mutation re-reads the file under
  an flock on `tasks.lock` and is a compare-and-set on the task's status; saving is atomic. A second chat
  may add, list, show and cancel; only the chat holding `tasks-owner.lock` claims, finishes and recovers
  (a task left `running` by a crashed chat becomes `failed`). A second chat takes the owner lock over
  as soon as the owner chat has closed (checked before each prompt and task command). A board update
  that fails after a helper finished is retried on later ticks; a `running` record no worker owns is
  ended. A board that becomes unreadable while the chat runs is set aside, reported in the chat, and the
  running helpers' records are put back instead of being treated as cancelled. Once the chat closes,
  no new helper starts: a claim that returns late goes back to the queue. Logs carry task ids, never text.
- **Helpers.** Each task gets its own one-shot `claude -p` session in the vault with the main session's
  hardening, `--no-session-persistence`, `--max-turns 30`, no plugins and no skills. Tools: Read, Glob,
  Grep, plus Edit only when the task has a domain. No Skill, Task or Agent tool (allowlist, deny rules
  and the init tripwire), no delegation tool, so a helper cannot start a helper. At most two run at
  once. The quota guard is checked before each claim and again right before sending, with the lower
  `PA_TASK_QUOTA_STOP`, so background work leaves headroom for the chat; without fresh quota data only
  one helper runs as a probe; at most 30 task starts per day. A watchdog stops a helper after
  `PA_TASK_TIMEOUT_S`.
- **One broker, many scopes (D2).** The main session and each helper have their own approval scope:
  token, MCP config file and turn state. A request is bound to a tool call announced in that very
  session's stream, so a helper's token can never approve a main-session call or the reverse. All
  scopes share one prompt queue, the main session first. A helper prompt waits at most two minutes,
  is withdrawn when the main session needs the terminal (a deny, but neither a refusal nor part of the
  helper's budget; the terminal says `[withdrawn: the chat needs the prompt]`), and a helper may show at
  most three prompts per task. Prompt headers carry a trusted label (`[approval · task 3f2a9c
  (research)]` versus `[approval · chat]`); the task title appears only quoted and ASCII-escaped.
- **Write rules.** Helpers write through an allowlist: the only write they may ask for is an Edit that
  appends up to three lines `- YYYY-MM-DD (task <own id>): finding` (at most 600 characters, no
  heading, Links line, checkbox or invisible character) to their own `wiki/domains/<domain>.md`; the
  prompt shows the complete appended text. Because Claude Code's Edit replaces `old_string` wherever it
  occurs, the broker reads the file itself (no symlinks, 128 KiB cap) and allows the Edit only when
  `old_string` is the file's tail, occurs nowhere else and starts at or after the `## Notes` heading; so
  the result is exactly the old file plus the shown lines, never an insertion into the Links line, the
  heading or an earlier note. Everything else, `outputs/` included, is denied without asking. Links
  between domains stay editable by the person only.
- **Results (D3).** The coordinator writes `outputs/tasks/<id>.md` with trusted code: a fixed header,
  the request, then the helper's answer with invisible characters removed and every heading of it
  (ATX with up to three spaces of indentation, setext underlines, raw HTML `<h1>`-`<h6>`) shown as plain
  text, so it cannot fake the header block. The board stores its sha256; `task show` flags a file changed
  since. The main session may never write `outputs/tasks/`, and a main turn that read task results or
  domain notes (or searched the whole vault) marks its later approval prompts like note context. Claude
  Code runs in-vault Read/Glob/Grep without asking the permission tool, so the mark is set from the
  tool calls announced in the stream; the same check on permission requests is only a backup. Domain
  notes are never attached to chat messages or to the brief.
- **Delegation (D4, off by default).** With `PA_TASK_DELEGATION=on` the main session's `pa` server also
  offers `delegate_task` (text up to 300 characters, optional domain). The `ask` rule routes it through
  the permission tool; the broker allows at most one per turn, raises it to level 3 when the turn
  carries note or skill context, and remembers the approved input digest. The delegation call itself
  must present exactly that input, once, in the same turn; if the CLI does not route the call through
  the permission tool, nothing is ever approved and every delegation is refused (fail-closed). The
  model gets only the task id. A delegated task is shown to the helper as `<delegated-request>`, never
  as the person's `<user-message>`, and is marked `[delegated]`. Delegation is offered (MCP flag,
  system prompt paragraph, init tripwire) only when the chat's task service started; without one the
  broker denies `delegate_task` without asking.
- **Domains (AP3.2).** A task may name a domain; its file `wiki/domains/<domain>.md` has a `Links:`
  line and an append-only `## Notes` section. Trusted code creates the skeleton on the first
  `task @<domain>` (O_EXCL, no symlinks, at most 50 domains). The helper gets its own notes, then those of
  linked domains (depth 1, at most five links, missing ones skipped), tail-trimmed to whole newest lines
  within `PA_DOMAIN_CONTEXT_CHARS` (own up to 60 % when linked notes come too), inside a
  `<domain-notes id="<random>">` block in front of the task; text, names and paths are escaped, so a
  note cannot close the block, forge a `<user-message>` or attributes. Oversized prompts drop the notes.
  Domain names are checked against `[a-z0-9]+(-[a-z0-9]+)*` (at most 32 characters) before any path is
  built, and reads never follow symlinks and stop at 128 KiB. A turn with notes carries the context hint
  on its approval prompts. Domain notes reach models only this way, never through the main session's
  excerpts or the brief (`vault.CONTEXT_SKIP`).
- **Notices.** The chat prints `[task <id> done: outputs/tasks/<id>.md]`, `failed`, `cancelled` and
  `[tasks waiting: <reason>]` through a queue, so a worker never waits for the terminal. With
  `PA_TASK_DISCORD=counts` the webhook gets at most one message per minute with counts only, never ids,
  titles, domains, errors or paths.
- **One safety latch.** All sessions share the Claude Code config dir and login, so a safety stop in
  any of them (hook event, unexpected init, disallowed skill, output outside a turn) halts the chat and
  aborts every running helper until restart.
- **Our own sessions and other sessions.** The Claude Code docs (cross-session messaging) say a `-p`
  session binds an inbox socket, so the user's other sessions could message it. Hence
  `crossSessionInbound: refuse` and the deny rules, plus a tripwire in the bridge: conversation output
  that arrives while no turn of ours runs stops the session before its events could be taken as an
  answer or bound to an approval.
- **Leftover processes.** Closing the chat aborts every helper (failed "interrupted (the chat was
  closed)", never requeued) and SIGKILLs a process group that ignored SIGTERM; signals are sent only to
  a child that was not yet reaped. If the chat itself is killed with SIGKILL, an orphaned helper keeps
  its own process group, every approval fails because the socket is gone, it ends at stdin EOF after its
  current turn, and `--max-turns` bounds that turn.

### 3e. Other Claude Code sessions

The person's other sessions belong to other projects and run with full user privileges, so the agent
only **reads** about them:

- **Sources.** By default the status files that `hooks/claude_status_hook.py` writes (UserPromptSubmit,
  PreToolUse, Notification, Stop, StopFailure → error, SessionEnd). `personal-agent hooks snippet`
  prints the settings snippet with absolute paths to the packaged copy of the hook; it never opens,
  reads or writes a settings file, because `~/.claude` is the person's territory. Optionally
  (`PA_SESSIONS_SOURCE=agents|both`) the documented `claude agents --json`, merged by session id with
  Claude Code's state winning. That source is off by default: agent view is a research preview, and
  whether the call starts its supervisor is undocumented.
- **Privacy boundary.** The agents listing covers every project. It is read only for the typed tier-1
  `sessions` command (never by `serve`, the scheduler, the waiting-session escalation, helpers or any
  model path; a test checks the call sites), with a minimal environment (no API keys, never the
  assistant's own config dir), stdin closed, a 5 s timeout with the process group killed, a 256 KiB
  output cap, at most 100 entries and a 10 s cache. Only the basename of a working directory is kept,
  every string is cleaned, and nothing from it is logged or sent to Discord or the phone.
- **View.** Waiting sessions first, then error, working, idle; at most 20 lines. `status` and the
  phone call keep their own order (errors first). Status files stay forgeable by any local process
  of the person, which is why they may inform but never authorize anything.
- **No control.** Research on 2026-10-18 (Claude Code 2.1.302) found no official, stable, documented
  way for a separate process to send into a running session it does not host or to answer its
  prompts. Session control is a documented design with threat model and required controls, marked
  not implemented, in [SESSION-CONTROL.md](SESSION-CONTROL.md); there is no switch for it.

### 4. Caller authentication

- v1 only places **outbound** calls to one configured number. Inbound calls are not accepted.
- Nothing about the alert is said before a **PIN** (at least 6 digits, DTMF only, so it is not spoken
  aloud) is entered. Three wrong attempts end the call; the call cannot be resumed.
- Call URLs carry a random, single-use id that expires after 15 minutes.
- Every Twilio webhook must carry a valid `X-Twilio-Signature`; the URL used for the check comes from
  configuration, not from request headers.

### 5. Network exposure

- The server binds to `127.0.0.1` by default. Only `/twilio/` and `/approve/` may be forwarded, through
  a reverse proxy or tunnel with TLS (for `/approve/`, tailnet-only access is preferred).
  `/escalate` and `/internal/` stay local and need a bearer token. Because many tunnel tools forward
  the whole port when no path is given, both also answer 404 unless the client is on loopback and the
  request has no forwarding header (`Forwarded`, `X-Forwarded-*`, `X-Real-IP`, `Tailscale-*`).
  In a container, host clients arrive from the bridge gateway instead of loopback; name it in
  `PA_TRUSTED_CLIENTS` (private networks only), otherwise `escalate` and remote approvals get 404.
  A whole-port tunnel running on the host would then also arrive from that gateway, so the
  forwarding-header check is the remaining guard there.
- Approval pages send `no-store`, `X-Frame-Options: DENY`, `nosniff`, a same-origin referrer policy and
  a CSP without scripts. Request sockets time out after 15 seconds.
- Local calls from the CLI to the server never go through an `HTTP_PROXY`, so the bearer token cannot
  leak to a proxy.
- Docker deployments must publish ports as `127.0.0.1:PORT:PORT`; a bare `PORT:PORT` bypasses host
  firewalls such as UFW.
- Request bodies are capped at 8 KiB.

### 6. Secrets

Secrets come only from environment variables (`.env`, never committed). `.gitignore` excludes `.env*`,
`secrets/` and key files. Logs never contain tokens, PINs or full phone numbers.

### 7. Cost and noise limits

Calls cost money and attention. Only `critical` events call; the same key calls at most once per hour;
there is a minimum interval between calls, a daily cap and optional quiet hours. Limits survive a
restart.

The assistant uses only the subscription, never paid usage. Before each chat turn the quota guard
checks the last `rate_limit_event` Claude Code reported. It refuses the turn without starting any
model when the status is not `allowed`, when paid overage is in use, or when the five-hour or seven-day
window has reached `PA_QUOTA_STOP` (default 90 %). A refusal lasts until the reported reset time, or
15 minutes when that time is unknown, so one probe turn can refresh stale data. Paid overage gets no
probe turn, because the probe would be billed: it blocks until the latest known reset, or, with none
known, until you type `quota reset`. If a session's init
event reports an API key source, or overage appears during a turn, the turn is aborted and the
process stopped. The router keeps usage low: `help`, `status` and `quota` are answered without a
model, short self-contained questions go to a small model with no tools, everything else to the main
session. A leading `!` forces the main session.

## Roadmap

| Version | Content | Main risk handled |
|---|---|---|
| **v1** | Escalation call, session status by phone, acknowledge, simulator | Noise and cost (limits), caller auth |
| phase 2 | Vault workspace with note capture and note excerpts (done); checked plugins (done); morning brief (done); core skills plugin (done) | Injection from note text (delimited, escaped excerpts; slash allowlist); code execution and injection through plugins (checker, hooks off, Skill allowlist, tripwire); private text in Discord and send storms (counts by default, escaping, once-per-day state under a lock) |
| phase 3 | Task board with background helpers, approval scopes, completion notices, optional self-delegation, domain memory files with links (done); a read-only `sessions` view of other Claude Code sessions and `hooks snippet` (done); session control documented as not implemented | Helpers starting helpers or writing outside their notes (tool allowlist, write allowlist, scopes); quota drain by unattended work (lower threshold, caps, expiry); injection through delegated text and results (approval, delimiting, no result back into the main session); privacy of other projects' sessions (basename only, opt-in agents source, never forwarded) |
| v2 | Conversational call (speech in/out via a realtime voice pipeline such as Pipecat or LiveKit Agents), optional LLM summary of status; sending to a waiting session only if Claude Code documents a stable mechanism (see SESSION-CONTROL.md) | Injection from session text into the voice model |
| v3 | Mail reader: classify and summarise mail, write reply **drafts**; approval flow (chat or phone) before sending | Injection from mail content, separate process and credentials |
| v4 | Voice dashboard (status, numbers, news) | Exposure of a web UI, auth |
| later | More providers (Telnyx, SIP), more languages, packaging (Docker image with 127.0.0.1 bindings) | — |

## Open questions

- Project name.
- Regulatory requirements for phone numbers per country (e.g. address verification) – documented
  per provider before v2.
- Which speech pipeline for v2 (latency, German quality, self-hostable parts).
- Whether Claude Code passes the permission tool exactly the input it announces in the stream. The
  broker binds each request to the announced tool call by an input hash; if the CLI normalizes the
  input (filled defaults, coerced types), every write is denied without a prompt. A mismatch is
  logged with the differing key names only. To be checked against the real CLI for Write and Edit,
  with and without optional keys, before relaxing the binding to id, name and path keys.
- Phase 3 spike against the real CLI (manual, never in CI): whether 2.1.302 accepts
  `crossSessionInbound` and deny rules for tools it may not offer without breaking startup; whether
  `refuse` is effective (`/list-agents` from another session cannot deliver to a bridge session) and
  whether bridge sessions appear there at all despite their own `CLAUDE_CONFIG_DIR` (the socket
  fallback directory is per user, not per config dir); whether `--max-turns` works with stream-json
  input; and whether the `ask` rule routes `mcp__pa__delegate_task` through the permission tool (if not,
  delegation stays refused, fail-closed). If a key breaks startup it is not simply dropped: the
  `--tools` allowlist and the out-of-turn tripwire stay, and bare mode is the alternative to evaluate.
  Record the results here.
- Whether `claude agents --json` starts the agent-view supervisor, and its exact field formats (e.g.
  `startedAt` as number or ISO string): undocumented, so the agents source stays opt-in and the reader
  accepts both. Check once by hand before recommending it.
