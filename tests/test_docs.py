"""AP3.3b: session control is documented as not implemented, and no code path sends to a session."""

import argparse
from pathlib import Path

from personal_agent.cli import COMMANDS, build_parser
from personal_agent.tasks import parse_command

ROOT = Path(__file__).resolve().parent.parent
SESSION_CONTROL = ROOT / "docs" / "SESSION-CONTROL.md"
MARKER = "Status: NOT IMPLEMENTED (design only; no code sends anything into another session)"


def test_session_control_doc_carries_the_not_implemented_marker():
    lines = SESSION_CONTROL.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# ")
    assert lines[1] == MARKER


def test_session_control_doc_records_the_open_spike_and_the_fallback():
    text = SESSION_CONTROL.read_text(encoding="utf-8")
    assert "crossSessionInbound" in text and "spike" in text
    assert "SendMessage" in text and "ListAgents" in text
    assert "tripwire" in text and "not** simply removed" in text
    assert "2.1.302" in text and "Threat model" in text and "level-3" in text
    for url in ("https://code.claude.com/docs/en/agent-view", "https://code.claude.com/docs/en/cross-session-messaging",
                "https://code.claude.com/docs/en/remote-control", "https://code.claude.com/docs/en/channels"):
        assert url in text


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:  # noqa: SLF001 - argparse has no public accessor
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            return dict(action.choices)
    return {}


def test_cli_has_no_command_that_sends_to_a_session():
    commands = _subcommands(build_parser())
    assert set(commands) == set(COMMANDS) == {
        "serve", "escalate", "status", "simulate", "chat", "vault", "plugins", "brief", "hooks", "telegram", "safety",
        "modules", "mail"}
    assert set(_subcommands(commands["telegram"])) == {"pair", "status", "unpair"}
    assert set(_subcommands(commands["hooks"])) == {"snippet"}
    assert set(_subcommands(commands["mail"])) == {"check"}  # read-only: nothing that sends mail


def test_chat_commands_have_no_session_send_kind():
    known = {"task_usage", "task_add", "tasks", "task_show", "task_cancel", "domains", "domain_show",
             "domain_usage", "sessions"}
    corpus = ["sessions", "sitzungen", "sessions send abc hi", "session send abc hi", "send abc hi",
              "reply abc yes", "answer abc yes", "attach abc", "sessions reply abc ok", "sessions abc",
              "task show 3f2a", "tasks", "domains", "domain x", "task do it"]
    kinds = {cmd.kind for cmd in map(parse_command, corpus) if cmd is not None}
    assert kinds <= known
    assert not any("send" in kind or "reply" in kind for kind in kinds)


def test_mail_commands_have_no_send_or_reply_kind():
    from personal_agent.mail import parse_mail_command

    corpus = ["mail", "mail refresh", "mail show 3f2a9c", "mail send 3f2a9c", "mail reply 3f2a9c ok",
              "reply 3f2a9c ok", "mail forward 3f2a9c x@example.org", "mail delete 3f2a9c", "mail move 3f2a9c x",
              "mail zeige 3f2a9c", "mail show 3F2A9C", "mail show xyz"]
    kinds = {cmd.kind for cmd in map(parse_mail_command, corpus) if cmd is not None}
    assert kinds == {"mail_overview", "mail_refresh", "mail_show", "mail_usage"}
    assert not any(word in kind for kind in kinds for word in ("send", "reply", "forward", "delete", "move"))


def test_readme_and_concept_point_to_the_snippet_and_the_design():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    concept = (ROOT / "docs" / "CONCEPT.md").read_text(encoding="utf-8")
    assert "personal-agent hooks snippet" in readme
    assert "PA_SESSIONS_SOURCE" in readme
    assert "SESSION-CONTROL.md" in readme and "SESSION-CONTROL.md" in concept
    assert "claude -p --resume" not in concept.split("## Architecture")[0]  # the old principle is gone
