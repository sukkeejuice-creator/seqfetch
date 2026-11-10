"""Streaming MD5: hash a file while another process is still writing it.

This is the core of seqfetch. Instead of reading 20-200 GiB back from disk after
the transfer (which can take longer than the transfer itself on slow HDDs), a
background thread tails the ``.part`` file, feeding new bytes into a running MD5
context. When curl exits, only the last few MiB remain and the checksum is
available almost immediately.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Optional


def _new_md5():
    try:
        return hashlib.md5(usedforsecurity=False)  # type: ignore[call-arg]
    except TypeError:  # pragma: no cover - old Python
        return hashlib.md5()


def md5_file(path: str, chunk: int = 8 * 1024 * 1024) -> str:
    digest = _new_md5()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class StreamingMD5(threading.Thread):
    """Tail ``path`` and maintain an MD5 of everything written so far.

    Safe against truncation/restart: if the file shrinks below the current
    position, the context is reset and hashing starts over.
    """

    def __init__(
        self,
        path: str,
        chunk: int = 8 * 1024 * 1024,
        poll: float = 0.2,
        settle_rounds: int = 25,
    ) -> None:
        super().__init__(daemon=True, name="seqfetch-md5")
        self.path = path
        self.chunk = chunk
        self.poll = poll
        self.settle_rounds = settle_rounds
        self._stop_evt = threading.Event()
        self._digest = _new_md5()
        self.pos = 0
        self.restarts = 0
        self.error = ""
        self.last_size = 0

    # -- lifecycle ------------------------------------------------------
    def run(self) -> None:  # noqa: D401 - thread body
        stable = 0
        while True:
            stopping = self._stop_evt.is_set()
            try:
                size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
            except OSError:
                size = 0
            self.last_size = size

            if size < self.pos:
                # Writer restarted/truncated the file; old bytes are gone.
                self._digest = _new_md5()
                self.pos = 0
                self.restarts += 1

            if size > self.pos:
                if not self._drain_to(size):
                    if stopping:
                        break
                    time.sleep(self.poll)
                    continue

            if stopping:
                # curl has exited already; wait for the page cache to settle.
                if size <= self.pos:
                    break
                stable += 1
                if stable > self.settle_rounds:
                    break
                time.sleep(self.poll)
            else:
                time.sleep(self.poll)

    def _drain_to(self, target: int) -> bool:
        try:
            with open(self.path, "rb") as f:
                f.seek(self.pos)
                while self.pos < target:
                    block = f.read(min(self.chunk, target - self.pos))
                    if not block:
                        break
                    self._digest.update(block)
                    self.pos += len(block)
        except OSError as exc:
            self.error = str(exc)
            return False
        return self.pos >= target

    def stop(self) -> None:
        self._stop_evt.set()

    # -- results --------------------------------------------------------
    @property
    def digest(self) -> str:
        return self._digest.hexdigest()

    @property
    def caught_up(self) -> bool:
        try:
            return self.pos == os.path.getsize(self.path)
        except OSError:
            return False

    def digest_if_complete(self, expected_size: Optional[int] = None) -> Optional[str]:
        """Return the digest only when every byte is accounted for."""
        if self.error:
            return None
        size = expected_size
        if size is None:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                return None
        if self.pos != size:
            return None
        return self.digest
