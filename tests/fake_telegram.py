"""A fake Telegram Bot API on 127.0.0.1 for tests: no network, no real bot.

POST /bot<token>/<method> with a JSON body. A wrong token gets 401. getUpdates
honours offset, limit and allowed_updates and long-polls on a condition for at most
`max_poll_s`. sendMessage returns increasing message ids and rejects texts over 4096
UTF-16 units. fail() injects error answers (429 with retry_after, 409, 401, 500,
garbage) for the next calls of a method.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "123456789:" + "T" * 35  # synthetic, has the shape of a bot token
BOT_ID = 123456789
OWNER = 4242


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class FakeBotAPI:
    def __init__(self, token: str = TOKEN, username: str = "pa_test_bot", bot_id: int = BOT_ID,
                 max_poll_s: float = 0.3) -> None:
        self.token = token
        self.username = username
        self.bot_id = bot_id
        self.max_poll_s = max_poll_s
        self.calls: list[tuple[str, dict]] = []
        self.paths: list[str] = []
        self.updates: list[dict] = []
        self.webhook_url = ""
        self._next_update = 1
        self._next_message = 100
        self._client_message: dict[int, int] = {}
        self._failures: dict[str, list[tuple[int, float | None, bytes | None, float]]] = {}
        self._cond = threading.Condition()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # quiet
                pass

            def do_POST(self) -> None:  # noqa: N802
                fake._handle(self)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)
        self._thread.start()

    # --- scripting ---

    def _push(self, update: dict) -> int:
        with self._cond:
            update_id = self._next_update
            self._next_update += 1
            self.updates.append({"update_id": update_id, **update})
            self._cond.notify_all()
            return update_id

    def push_message(self, chat_id: int = OWNER, user_id: int | None = None, text: str | None = None,
                     chat_type: str = "private", date: int | None = None, is_bot: bool = False,
                     message_id: int | None = None, **extra) -> int:
        user_id = chat_id if user_id is None else user_id
        if message_id is None:
            message_id = self._client_message.get(chat_id, 0) + 1
        self._client_message[chat_id] = max(self._client_message.get(chat_id, 0), message_id)
        message = {"message_id": message_id, "date": int(time.time()) if date is None else date,
                   "chat": {"id": chat_id, "type": chat_type}, "from": {"id": user_id, "is_bot": is_bot,
                                                                         "username": "owner_user"}}
        if text is not None:
            message["text"] = text
        message.update(extra)
        return self._push({"message": message})

    def push_callback(self, user_id: int, chat_id: int, message_id: int, data: str,
                      query_id: str | None = None) -> int:
        with self._cond:
            qid = query_id or f"cq{self._next_update}"
        return self._push({"callback_query": {"id": qid, "from": {"id": user_id, "is_bot": False},
                                              "message": {"message_id": message_id, "chat": {"id": chat_id,
                                                                                            "type": "private"}},
                                              "data": data}})

    def fail(self, method: str, status: int, *, retry_after: float | None = None, times: int = 1,
             body: bytes | None = None, delay: float = 0.0) -> None:
        with self._cond:
            self._failures.setdefault(method, []).extend([(status, retry_after, body, delay)] * times)

    def wait_for(self, method: str, pred=lambda body: True, timeout: float = 5.0, count: int = 1) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._cond:
                found = [body for name, body in self.calls if name == method and pred(body)]
            if len(found) >= count:
                return found[count - 1]
            time.sleep(0.02)
        raise AssertionError(f"no {method} call matched within {timeout} s")

    def calls_of(self, method: str) -> list[dict]:
        with self._cond:
            return [body for name, body in self.calls if name == method]

    def sent_texts(self, chat_id: int | None = None) -> list[str]:
        return [b["text"] for b in self.calls_of("sendMessage") if chat_id is None or b.get("chat_id") == chat_id]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # --- the API ---

    def _reply(self, handler: BaseHTTPRequestHandler, status: int, payload: object | bytes) -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        except OSError:
            pass

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        length = int(handler.headers.get("Content-Length") or 0)
        raw = handler.rfile.read(length)
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}
        path = handler.path
        prefix = f"/bot{self.token}/"
        with self._cond:
            self.paths.append("/bot<token>/" + path.rsplit("/", 1)[-1] if path.startswith(prefix) else "other")
        if not path.startswith(prefix):
            self._reply(handler, 401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
            return
        method = path[len(prefix):]
        with self._cond:
            self.calls.append((method, body))
            queued = self._failures.get(method)
            failure = queued.pop(0) if queued else None
        if failure is not None:
            status, retry_after, fbody, delay = failure
            if delay:
                time.sleep(delay)
            if fbody is not None:
                self._reply(handler, status, fbody)
                return
            payload: dict = {"ok": False, "error_code": status, "description": f"error {status}"}
            if retry_after is not None:
                payload["parameters"] = {"retry_after": retry_after}
            self._reply(handler, status, payload)
            return
        result = getattr(self, f"_m_{method}", None)
        if result is None:
            self._reply(handler, 404, {"ok": False, "error_code": 404, "description": "Not Found"})
            return
        outcome = result(body)
        if isinstance(outcome, tuple):
            self._reply(handler, outcome[0], outcome[1])
        else:
            self._reply(handler, 200, {"ok": True, "result": outcome})

    def _m_getMe(self, body: dict) -> dict:
        return {"id": self.bot_id, "is_bot": True, "username": self.username, "first_name": "PA"}

    def _m_getWebhookInfo(self, body: dict) -> dict:
        return {"url": self.webhook_url, "has_custom_certificate": False, "pending_update_count": 0}

    def _m_getUpdates(self, body: dict) -> list:
        offset = body.get("offset") or 0
        limit = int(body.get("limit") or 100)
        allowed = body.get("allowed_updates")
        timeout = min(float(body.get("timeout") or 0), self.max_poll_s)
        end = time.monotonic() + timeout
        with self._cond:
            while True:
                self.updates = [u for u in self.updates if u["update_id"] >= offset]
                ready = [u for u in self.updates
                         if not allowed or any(k in u for k in allowed)]
                if ready or time.monotonic() >= end:
                    return ready[:limit]
                self._cond.wait(max(0.0, end - time.monotonic()))

    def _m_sendMessage(self, body: dict):
        text = body.get("text")
        if not isinstance(text, str) or not text or utf16_len(text) > 4096:
            return 400, {"ok": False, "error_code": 400, "description": "Bad Request: message is too long"}
        with self._cond:
            self._next_message += 1
            message_id = self._next_message
            body["_message_id"] = message_id  # for tests: the id this call returned
        return {"message_id": message_id, "chat": {"id": body.get("chat_id"), "type": "private"}, "text": text}

    def _m_answerCallbackQuery(self, body: dict) -> bool:
        return True

    def _m_editMessageReplyMarkup(self, body: dict) -> bool:
        return True

    def _m_editMessageText(self, body: dict) -> bool:
        return True

    def _m_deleteMessage(self, body: dict) -> bool:
        return True
