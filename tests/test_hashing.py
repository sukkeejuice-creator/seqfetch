from __future__ import annotations

import hashlib
import os
import time

from seqfetch.hashing import StreamingMD5, md5_file


def test_streaming_matches_one_shot(tmp_path):
    path = tmp_path / "grow.bin"
    hasher = StreamingMD5(str(path), chunk=1024 * 1024, poll=0.01, settle_rounds=5)
    hasher.start()
    digest = hashlib.md5()
    with open(path, "wb") as handle:
        for i in range(8):
            block = bytes([i]) * (512 * 1024)
            handle.write(block)
            handle.flush()
            digest.update(block)
            time.sleep(0.02)
    hasher.stop()
    hasher.join(timeout=10)
    assert not hasher.is_alive()
    assert hasher.digest_if_complete() == digest.hexdigest() == md5_file(str(path))


def test_streaming_restarts_after_truncate(tmp_path):
    path = tmp_path / "restart.bin"
    path.write_bytes(b"old-data" * 1000)
    hasher = StreamingMD5(str(path), chunk=4096, poll=0.01, settle_rounds=5)
    hasher.start()
    time.sleep(0.1)

    path.write_bytes(b"")
    time.sleep(0.1)
    replacement = b"new" * 50_000
    with open(path, "wb") as handle:
        handle.write(replacement)
    hasher.stop()
    hasher.join(timeout=10)

    assert hasher.restarts >= 1
    assert hasher.digest_if_complete() == hashlib.md5(replacement).hexdigest()


def test_empty_file(tmp_path):
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")
    hasher = StreamingMD5(str(path), poll=0.01)
    hasher.start()
    hasher.stop()
    hasher.join(timeout=5)
    assert hasher.digest_if_complete() == hashlib.md5(b"").hexdigest()
