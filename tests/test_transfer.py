from __future__ import annotations

import hashlib
import os

from conftest import base_transfer_config
from seqfetch.transfer import fetch


def _make_file(root, name, size, seed):
    data = bytes([seed]) * size
    path = root / name
    path.write_bytes(data)
    return str(path), hashlib.md5(data).hexdigest(), size


def test_fetch_streams_md5(http_dir):
    server, handler, root = http_dir
    path, md5, size = _make_file(root, "sample.fq.gz", 4 * 1024 * 1024, 7)
    dest = os.path.join(str(root), "out", "sample.fq.gz")

    outcome = fetch(
        f"http://127.0.0.1:{server.server_address[1]}/sample.fq.gz",
        dest,
        size,
        md5,
        base_transfer_config(),
    )

    assert outcome.ok, outcome.error
    assert outcome.status == "OK_MD5"
    assert outcome.md5 == md5
    assert outcome.md5_ok is True
    assert outcome.hash_source == "stream"
    assert os.path.getsize(dest) == size
    assert not os.path.exists(dest + ".part")


def test_fetch_resumes_existing_part(http_dir):
    server, handler, root = http_dir
    path, md5, size = _make_file(root, "resume.fq.gz", 8 * 1024 * 1024, 9)
    dest = os.path.join(str(root), "out", "resume.fq.gz")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest + ".part", "wb") as handle:
        handle.write(open(path, "rb").read()[: 3 * 1024 * 1024])

    outcome = fetch(
        f"http://127.0.0.1:{server.server_address[1]}/resume.fq.gz",
        dest,
        size,
        md5,
        base_transfer_config(),
    )

    assert outcome.ok, outcome.error
    assert outcome.status == "OK_MD5"
    assert outcome.md5 == md5
    assert any((value or "").startswith("bytes=") for value in handler.requests)
    assert os.path.getsize(dest) == size


def test_md5_mismatch_discards_partial(http_dir):
    server, handler, root = http_dir
    _, _, size = _make_file(root, "bad.fq.gz", 2 * 1024 * 1024, 3)
    dest = os.path.join(str(root), "out", "bad.fq.gz")

    outcome = fetch(
        f"http://127.0.0.1:{server.server_address[1]}/bad.fq.gz",
        dest,
        size,
        "0" * 32,
        base_transfer_config(**{"transfer.attempts": 1}),
    )

    assert not outcome.ok
    assert outcome.status == "MD5_MISMATCH"
    assert not os.path.exists(dest)
    assert not os.path.exists(dest + ".part")


def test_existing_file_is_reused(http_dir):
    server, handler, root = http_dir
    path, md5, size = _make_file(root, "exists.fq.gz", 1024 * 1024, 1)
    dest = os.path.join(str(root), "out", "exists.fq.gz")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(path, "rb") as src, open(dest, "wb") as dst:
        dst.write(src.read())

    outcome = fetch(
        f"http://127.0.0.1:{server.server_address[1]}/exists.fq.gz",
        dest,
        size,
        md5,
        base_transfer_config(),
    )

    assert outcome.ok
    assert outcome.status == "EXISTS"
    assert outcome.md5 == md5
    assert handler.requests == []
