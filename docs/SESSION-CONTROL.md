# Session control
Status: NOT IMPLEMENTED (design only; no code sends anything into another session)

This document records why personal-agent does not send messages into the person's other Claude Code
sessions, what it does instead, and which controls a future implementation would need. There is no
configuration switch for it, and the chat has no command that sends to a session.

## 1. Question

Can the agent safely send a message to one of the person's waiting Claude Code sessions, or answer
its prompt (a permission request, a question, a dialog)?

## 2. Findings

Checked on 2026-10-18 against Claude Code 2.1.302: the documentation on code.claude.com and the
`--help` output of the CLI and its subcommands. No model was started and no session was listed or
messaged for this check.

| Mechanism | Documented | Reaches a running session we do not host | Can answer a permission prompt | Stable | Verdict |
|---|---|---|---|---|---|
| [`claude agents --json`](https://code.claude.com/docs/en/agent-view) | yes ("the supported way to read session state from outside Claude Code") | read-only | no | research preview | **observe only**; optional source of the `sessions` view |
| [Hooks](https://code.claude.com/docs/en/hooks) (Notification, Stop, StopFailure, ...) | yes | read-only (push) | only a PermissionRequest hook, and only from inside the target session's own settings | yes | **observe**; this is what `hooks/claude_status_hook.py` uses |
| [Cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging) (SendMessage / ListAgents) | yes | Claude to Claude only: the sender must itself be a session | no ("never counts as your consent") | yes | not usable by a separate process |
| [Inbox socket](https://code.claude.com/docs/en/cross-session-messaging) (`CLAUDE_CODE_MESSAGING_SOCKET`) | partly: only the auth line is documented | only via an undocumented message line; the path is exported only to the session's own children | no | no (the schema would have to be reverse-engineered) | **rejected** |
| [`claude --resume` / `claude -p --resume`](https://code.claude.com/docs/en/sessions) on a running session | yes | background: exits 1 and sends nothing with piped I/O or flags such as `--output-format`, `--settings`, `--max-turns`; interactive: not prevented, but [both processes write to one transcript](https://code.claude.com/docs/en/desktop) | no | no (interleaving) | **rejected** |
| [Background `attach` and peek reply](https://code.claude.com/docs/en/agent-view) | yes | interactive terminal only; a peek reply does not answer a dialog | no | research preview | for the person, not for a program |
| [Remote Control](https://code.claude.com/docs/en/remote-control) | yes | human-facing, through Anthropic's cloud; no public API for third-party processes | for the person on their device | yes | for the person, not for a program |
| [Channels](https://code.claude.com/docs/en/channels) (`claude/channel`, permission relay) | yes | only sessions started with `--channels` (opt-in at launch, plugin allowlist) | yes, if relayed and opted in | research preview | needs the person's launch-time opt-in; not a way into existing sessions |
| [Agent SDK](https://code.claude.com/docs/en/agent-sdk/typescript) and `--input-format stream-json` | yes | only sessions this process starts and owns | yes (`canUseTool`, `--permission-prompt-tool`), for our own sessions | yes | what the agent already does for **its own** sessions |
| Writing to a terminal, editing transcript files | no | would bypass every documented boundary | – | no | **never** |

## 3. Decision

No official, stable, documented mechanism lets a separate process send a message into a running
session it does not host without undocumented sockets or changes to the person's settings. Session
control is therefore **not implemented**, and there is no configuration switch that would turn it on.
Independently of that, the agent never touches sessions of other projects: they belong to other work
and run with the person's full privileges.

## 4. What the agent does instead

- **Observe.** The chat command `sessions` lists the person's sessions, waiting ones first, from the
  status files the hook writes (`personal-agent hooks snippet` prints the settings snippet; the
  command never edits a settings file). With `PA_SESSIONS_SOURCE=agents` or `both` it also reads the
  documented `claude agents --json` (off by default: research preview, it lists every project, and it
  is not documented whether it starts the agent-view supervisor). Only the basename of a working
  directory is shown; nothing from that listing is logged or sent to Discord or the phone, and it is
  read only for the typed command.
- **Escalate.** The existing session watch calls the person when a session has waited longer than
  `PA_WAITING_ESCALATE_MIN`.
- **Point the person to the answer.** Answering stays with the person: attach in a terminal
  (`claude attach <id>` or the session's own terminal), agent view, or Remote Control on the phone.
  The agent only says so in text.

## 5. Threat model (for any future implementation)

- **Assets.** The other sessions run with the person's full privileges in other projects. A message
  to them is equivalent to the person typing it.
- **Adversaries.** Injected text in notes, mail or web pages that reaches the assistant or a helper;
  a forged status file (any local process of the person can write the status directory); the holder
  of a leaked approval link; another local process.
- **Threats.**
  - Injection to command: the model composes the message, so data becomes an instruction to a
    privileged session.
  - Confused deputy: answering a permission prompt grants tool use in another project.
  - Wrong target or spoofed session (status files are forgeable; names can collide).
  - Replay of an approved message.
  - Transcript interleaving or corruption when two processes write to one conversation.
  - Privacy leaks of session names, working directories and first prompts to Discord or the phone.
  - Loops between the assistant and a session that answer each other.

## 6. Required controls if a documented mechanism appears

- Off by default; a new explicit opt-in variable, introduced only together with the mechanism.
- A tier-1 command typed by the person only; never from tier-2/3 tool calls, helpers or delegated
  tasks.
- The message text is typed verbatim by the person and shown in full (ASCII-escaped) in a **level-3**
  approval with the typed `confirm`; remotely also `PA_APPROVAL_PIN`.
- Never answer permission prompts or dialogs; only send messages.
- The target must be listed as waiting by a *supported* source (not status files alone, because they
  are forgeable), and its state is re-validated right before sending.
- A per-project allowlist of targets.
- No leading `/` or `!`, at most 2000 characters, one message per approval, a rate limit (5 per hour).
- An audit log of the target id and the message sha256, never the content.
- The receiver's `crossSessionInbound` is respected, with no bypass.

## 7. What would have to change

One of: a documented message schema plus discovery for the inbox socket, an official `send` CLI
command, or a stable channels protocol usable without a launch-time opt-in. Re-evaluate on Claude Code
releases and record the version checked here (last checked: 2.1.302 on 2026-10-18).

## 8. Our own sessions

The research also concerned the agent's own `claude -p` sessions (the chat, the small tier-2
session and the task helpers). The docs say a `-p` session binds an inbox socket like an interactive
one (only bare mode skips it), so the person's other sessions could list it and message it, and a
message would be delivered because these sessions count as prompting. `UserPromptSubmit` would also
fire for such a message. Therefore `bridge.HARDENED_SETTINGS` sets `crossSessionInbound: refuse` and
denies `SendMessage`, `ListAgents`, `Task` and `Agent`, and the `--tools` allowlist never offers them.

These keys come from the documentation and still await a manual spike against the real CLI (never in
CI): whether 2.1.302 accepts them without breaking startup, whether `refuse` is effective, and whether
bridge sessions appear in another session's `/list-agents` despite their own `CLAUDE_CONFIG_DIR` (the
socket fallback directory `/tmp/cc-socks-<uid>` is per user, not per config dir). If a key breaks
startup, it is **not** simply removed: the `--tools` allowlist (outbound) stays, the out-of-turn
tripwire in the bridge (inbound: conversation output that arrives while no turn of ours runs is a
safety stop) stays, and bare mode is the alternative to evaluate. The results go into
[CONCEPT.md](CONCEPT.md#open-questions).
