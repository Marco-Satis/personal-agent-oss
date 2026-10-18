"""The chat terminal: one owner of stdin for the `you>` prompt and the approval prompts.

While the person sits idle at `you>`, a background helper may need an approval. The
console therefore never blocks on input while holding the terminal: it reads with
short select() polls under the console lock, and a channel that needs the terminal
announces it with `wanted()`, so the input loop yields within one poll.

Notices from worker threads (a task finished) never wait for the terminal: they go
into a queue that the console prints whenever it holds the lock.

Trade-offs in interactive mode: readline history and arrow keys are not available,
and a partly typed message is discarded when an approval prompt appears (the prompt
says so).
"""

from __future__ import annotations

import contextlib
import os
import queue
import sys
import threading
import time
from typing import Callable, Iterator

POLL_S = 0.5


def _stdin_is_terminal() -> bool:
    try:
        return os.isatty(sys.stdin.fileno())
    except (AttributeError, ValueError, OSError):
        return False


def _write_prompt(prompt: str) -> None:
    sys.stdout.write(prompt)
    sys.stdout.flush()


class ChatConsole:
    """Owns the terminal for the chat loop; shares its lock with the approval channels."""

    def __init__(
        self,
        lock: threading.Lock,
        interactive: bool | None = None,
        read_line: Callable[[float], str | None] | None = None,
        write: Callable[[str], None] = print,
        poll_s: float = POLL_S,
        write_prompt: Callable[[str], None] | None = None,
    ) -> None:
        self.lock = lock
        # Decided once, from the real file descriptor (not approval.stdin_is_tty, which
        # tests patch while still feeding the chat through input()).
        self.interactive = _stdin_is_terminal() if interactive is None else interactive
        self._read_line = read_line
        self._write = write
        self._write_prompt = write_prompt or (_write_prompt if write is print else write)
        self.poll_s = poll_s
        self.reading = False  # True while read_message waits at the prompt
        self._wanted = 0
        self._wanted_lock = threading.Lock()
        self._dirty = False
        self._notices: queue.SimpleQueue[str] = queue.SimpleQueue()

    @contextlib.contextmanager
    def wanted(self) -> Iterator[None]:
        """A channel needs the terminal: the input loop stops taking the lock meanwhile."""
        with self._wanted_lock:
            self._wanted += 1
        try:
            yield
        finally:
            with self._wanted_lock:
                self._wanted -= 1
            self._dirty = True

    def _is_wanted(self) -> bool:
        with self._wanted_lock:
            return self._wanted > 0

    def notice(self, line: str) -> None:
        """Queue a line from any thread; never blocks. Printed when the console holds the lock."""
        self._notices.put(line)

    def _flush_notices(self) -> bool:
        """Print queued notices. The caller holds the lock."""
        printed = False
        while True:
            try:
                line = self._notices.get_nowait()
            except queue.Empty:
                return printed
            if not printed and self.reading:
                self._write("")
            self._write(line)
            printed = True

    def flush(self, timeout_s: float = 2.0) -> None:
        """Print pending notices now (e.g. at exit)."""
        if self.lock.acquire(timeout=timeout_s):
            try:
                self._flush_notices()
            finally:
                self.lock.release()

    def say(self, line: str) -> None:
        with self.lock:
            self._write(line)
            self._flush_notices()
            self._dirty = True

    def read_message(self, prompt: str) -> str | None:
        """One line from the person; None at end of input."""
        if not self.interactive:
            self.flush()
            try:
                return input(prompt)
            except EOFError:
                return None
        from .approval import read_stdin_line  # approval imports nothing from here

        reader = self._read_line or read_stdin_line
        with self.lock:
            self._flush_notices()
            self._write_prompt(prompt)
            self._dirty = False
        self.reading = True
        try:
            while True:
                if self._is_wanted():
                    time.sleep(self.poll_s)
                    self._dirty = True
                    continue
                if not self.lock.acquire(timeout=self.poll_s):
                    self._dirty = True
                    continue
                try:
                    if self._flush_notices():
                        self._dirty = True
                    if self._dirty:
                        self._write_prompt(prompt)
                        self._dirty = False
                    try:
                        line = reader(self.poll_s)
                    except EOFError:
                        return None
                finally:
                    self.lock.release()
                if line is not None:
                    return line
        finally:
            self.reading = False
