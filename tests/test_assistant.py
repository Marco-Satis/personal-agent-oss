"""The shared assistant construction (assistant.py) for the chat and serve's Telegram assistant."""

from pathlib import Path

from personal_agent import assistant, cli
from personal_agent.bridge import TurnResult
from personal_agent.router import LOCAL_ONLY, Reply, Router, RouterConfig
from personal_agent.vault import Excerpt, Vault, render_context
from tg_helpers import make_config


def test_cli_keeps_the_moved_names():
    assert cli._workdir_problem is assistant.workdir_problem
    assert cli.CHAT_TOOLS == assistant.CHAT_TOOLS and cli.STATE_ENTRIES == assistant.STATE_ENTRIES
    for name in ("telegram-owner.json", "telegram-pairing.json", "telegram-offset.json", "telegram.lock",
                 "safety-stop.json", "approval-pinlock.json"):
        assert name in assistant.STATE_ENTRIES


def test_the_telegram_runtime_is_the_chat_runtime_with_less_reach(tmp_path):
    from personal_agent.remote import ApprovalInbox
    from personal_agent.telegram_bot import SERVE_HINT, TelegramChannel, build_telegram_runtime

    config = make_config(tmp_path)
    inbox = ApprovalInbox(None, None, pin="731946")
    runtime = build_telegram_runtime(config, inbox)
    try:
        router, broker = runtime.router, runtime.parts.broker
        assert broker.main.label == "telegram" and not broker.delegation_allowed
        assert isinstance(broker.channel, TelegramChannel)
        assert router.sessions is None and router.local_listings is False and router.note_source == "telegram"
        assert router.tasks.origin == "telegram" and router.tasks.runner_hint == SERVE_HINT
        assert router.tasks.coordinator is None  # board only: serve runs no helpers
        assert runtime.main_session.config.extra_tools == ()
        assert router.latch.shared and router.latch.state_dir == config.state_dir
    finally:
        runtime.close()
    chat_parts = assistant.prepare_assistant(make_config(tmp_path / "c"), label="chat", channel=lambda b: None,
                                             note=lambda line: None)
    try:
        assert chat_parts.broker.main.label == "chat" and chat_parts.broker.delegation_allowed
    finally:
        chat_parts.broker.close()


def test_reply_lines_for_the_terminal_and_for_telegram(tmp_path):
    home = str(Path.home())
    result = TurnResult(f"Not logged in · see {home}/x", "s", True, "error_during_execution",
                        [{"tool_name": "Write"}], 0.0, None)
    reply = Reply(f"files in {home}/notes", 3, is_error=True, result=result, context_count=2)
    local = assistant.reply_lines(reply, Path("/cfg"))
    assert local[0] == "[context: 2 notes]" and local[1] == f"agent[3]> files in {home}/notes"
    assert "[not approved: Write]" in local and "/cfg" in local[-1]
    remote = assistant.reply_lines(reply, Path("/cfg"), prefix="", local_paths=False)
    assert remote[1] == "files in ~/notes" and all(home not in line for line in remote)
    assert "/cfg" not in remote[-1] and "setup-token" in remote[-1]
    refused = assistant.reply_lines(Reply("quota", 1, refused=True), Path("/cfg"), prefix="")
    assert refused == ["[quota] quota"]


class NoSession:
    def send(self, *a, **k):
        raise AssertionError("no model")

    def close(self, timeout_s=10):
        pass


class Guard:
    def check(self, *a, **k):
        raise AssertionError("no quota check for tier 1")

    def describe(self):
        return "quota"


def test_local_listings_stay_local():
    called = []
    router = Router(NoSession(), None, Guard(), RouterConfig(small_model=None),
                    statuses=lambda: called.append(1) or [], local_listings=False)
    assert router.handle("status").text == LOCAL_ONLY
    assert router.handle("sessions").text == LOCAL_ONLY
    assert called == []
    assert router.is_tier1("status") and router.is_tier1("note x") and router.is_tier1("tasks")
    assert not router.is_tier1("write me a poem about the sea")


def test_telegram_notes_are_marked_in_context(tmp_path):
    vault = Vault(tmp_path / "v")
    vault.init()
    path = vault.capture("call the bank", source="telegram")
    assert "[via telegram] call the bank" in (tmp_path / "v" / path).read_text()
    text = render_context("q", [Excerpt("wiki/a.md", 1, 2, "- 10:00 [via telegram] pay x", 1.0)], nonce="n")
    assert '<excerpt path="wiki/a.md" lines="1-2" source="telegram">' in text
    plain = render_context("q", [Excerpt("wiki/a.md", 1, 2, "plain", 1.0)], nonce="n")
    assert 'source=' not in plain


def test_telegram_queued_tasks_are_announced_and_labelled(tmp_path):
    import time

    from personal_agent.approval import ApprovalBroker
    from personal_agent.coordinator import Coordinator, TaskService, notice_line
    from personal_agent.quota import QuotaGuard
    from personal_agent.router import SafetyLatch
    from personal_agent.tasks import Command, TaskBoard

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    vault = Vault(tmp_path / "vault")
    vault.init()
    broker = ApprovalBroker(vault.root, channel=None, bind_wait_s=0.3)
    broker.start(tmp_path / "run")
    board = TaskBoard(state)
    phone = TaskService(board, None, SafetyLatch(), vault, origin="telegram", runner_hint="later")
    text, is_error = phone.command(Command("task_add", "look up the train times", None))
    assert not is_error and text.endswith("· later")
    assert board.list()[0].origin == "telegram"
    off = TaskService(board, None, SafetyLatch(), vault, origin="telegram", allow_add=False)
    assert off.command(Command("task_add", "x", None)) == ("tasks from Telegram are off (PA_TELEGRAM_TASKS=off)", True)
    labels, events = [], []

    class Session:
        session_id = None
        running = False
        config = None

        def send(self, body, on_event=None):
            from personal_agent.bridge import TurnResult

            return TurnResult("done", "s", False, "success", [], 0.0, None)

        def close(self, timeout_s=10):
            pass

    def make_session(task, scope):
        labels.append(scope.label)
        return Session()

    guard = QuotaGuard(0.9, None)
    guard.observe({"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.1},
                                                           "seven_day": {"utilization": 0.1}}})
    coordinator = Coordinator(board, broker, guard, SafetyLatch(), vault, make_session, events.append, tick_s=0.05)
    try:
        assert coordinator.start()
        end = time.monotonic() + 10
        while not labels and time.monotonic() < end:
            time.sleep(0.02)
        task_id = board.list()[0].id
        assert labels == [f"task {task_id} · telegram"]
        assert notice_line(events[0]) == f"[tasks: task {task_id} was queued from Telegram: look up the train times]"
    finally:
        coordinator.close(join_s=5)
        broker.close()
