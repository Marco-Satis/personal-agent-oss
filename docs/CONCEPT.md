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
| `cli.py` | `serve`, `escalate`, `status`, `simulate` |

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

### 4. Caller authentication

- v1 only places **outbound** calls to one configured number. Inbound calls are not accepted.
- Nothing about the alert is said before a **PIN** (at least 6 digits, DTMF only, so it is not spoken
  aloud) is entered. Three wrong attempts end the call; the call cannot be resumed.
- Call URLs carry a random, single-use id that expires after 15 minutes.
- Every Twilio webhook must carry a valid `X-Twilio-Signature`; the URL used for the check comes from
  configuration, not from request headers.

### 5. Network exposure

- The server binds to `127.0.0.1` by default. Only `/twilio/` may be forwarded to the internet, through
  a reverse proxy or tunnel with TLS. `/escalate` stays local and additionally needs a bearer token.
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
