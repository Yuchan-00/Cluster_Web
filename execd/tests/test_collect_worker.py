"""Swap races in the collect worker, made deterministic: an entry that scandir saw as a regular
file is replaced before open (security.md 20, Phase 1 "바꿔치기" check)."""

import io
import json
import os

from cluster_execd.collect_worker import Collector


def make(out=None, **kw):
    kw.setdefault("max_files", 10)
    kw.setdefault("max_total", 10_000)
    kw.setdefault("max_file", 1_000)
    return Collector(out or io.BytesIO(), ["*"], **kw)


def frames(buf):
    lines, data = [], buf.getvalue()
    while data:
        line, _, data = data.partition(b"\n")
        header = json.loads(line)
        lines.append(header)
        data = data[header.get("size", 0) :]
    return lines


def test_file_swapped_for_symlink_after_scandir(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("top secret")
    work = tmp_path / "work"
    work.mkdir()
    os.symlink(str(secret), str(work / "result.txt"))  # what open() now finds
    buf = io.BytesIO()
    c = make(buf)
    fd = os.open(str(work), os.O_RDONLY | os.O_DIRECTORY)
    try:
        c.collect_file(fd, "result.txt", "result.txt")
    finally:
        os.close(fd)
    (frame,) = frames(buf)
    assert frame["skip"] == "result.txt" and frame["reason"].startswith("open_failed:")
    assert b"top secret" not in buf.getvalue()


def test_file_swapped_for_fifo_does_not_block(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    os.mkfifo(str(work / "result.txt"))
    buf = io.BytesIO()
    fd = os.open(str(work), os.O_RDONLY | os.O_DIRECTORY)
    try:
        make(buf).collect_file(fd, "result.txt", "result.txt")  # O_NONBLOCK: returns at once
    finally:
        os.close(fd)
    assert frames(buf) == [{"skip": "result.txt", "reason": "not_regular"}]


def test_file_swapped_for_hardlink(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a").write_text("data")
    os.link(str(work / "a"), str(work / "b"))
    buf = io.BytesIO()
    fd = os.open(str(work), os.O_RDONLY | os.O_DIRECTORY)
    try:
        make(buf).collect_file(fd, "b", "b")
    finally:
        os.close(fd)
    assert frames(buf) == [{"skip": "b", "reason": "hardlink"}]


def test_growing_file_is_capped_at_open_time_budget(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "big").write_bytes(b"x" * 5000)
    buf = io.BytesIO()
    fd = os.open(str(work), os.O_RDONLY | os.O_DIRECTORY)
    try:
        make(buf, max_file=100).collect_file(fd, "big", "big")
    finally:
        os.close(fd)
    (frame,) = frames(buf)
    assert frame == {"file": "big", "size": 100, "truncated": True}
