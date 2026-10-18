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
- **Built on Claude Code itself**, not on a second agent framework. Session control uses what Claude
  Code already offers (hooks, headless `claude -p --resume`).
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
| `status.py` | Reads session status files, cleans every text field |
| `escalation.py` | Decides whether an event may call: severity, de-duplication, interval, daily cap, quiet hours, acknowledgements |
| `conversation.py` | The call as a deterministic state machine: PIN, alert, menu |
| `telephony.py` | Fake provider, Twilio REST call, TwiML rendering, webhook signature check |
| `server.py` | Local HTTP server, session watch thread |
| `bridge.py` | One locked-down Claude Code process per conversation (stream-json) |
| `approval.py` | Approval levels, the broker behind a private Unix socket, the terminal prompt |
| `approval_server.py` | The permission tool Claude Code calls (stdio MCP server, standard library only) |
| `quota.py` | Quota guard: refuses a turn before any model call when the subscription window is nearly used up or paid usage shows up |
| `router.py` | Three tiers: fixed answers, a small model without tools, the main locked-down session |
| `remote.py` | Remote approvals: Discord webhook notice, one-time approval page, the chat-side channel |
| `cli.py` | `serve`, `escalate`, `status`, `simulate`, `chat` |

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
| Session controller (v2) | session list | send a message to a session **after your confirmation** |

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

Prompts are shown one at a time. At most three requests may queue; after two refusals in one turn
the rest of that turn is denied. The prompt renders values as ASCII-escaped JSON, so control, bidi
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
| v2 | Conversational call (speech in/out via a realtime voice pipeline such as Pipecat or LiveKit Agents), optional LLM summary of status; send a message to a waiting session after spoken confirmation + PIN | Injection from session text into the voice model |
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
