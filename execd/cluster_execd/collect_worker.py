"""Collects output files from a run's work directory. Runs as cluster-run (never as root).

Started by execd through setpriv, so a symlink or hardlink planted by the job can only ever
reach files cluster-run could read anyway. On top of that, only regular files with a single
link that cluster-run owns are read, checked on the opened descriptor (no check-then-open race).

stdout frames (read by collect.py):
  {"file": <relpath>, "size": n, "truncated": bool}\\n  followed by n raw bytes
  {"skip": <relpath>, "reason": str}\\n
  {"end": true}\\n
Usage: python3 -I collect_worker.py <workdir> <plan-json>  (stdlib only, no package imports)
"""

from __future__ import annotations

import fnmatch
import json
import os
import stat
import sys
from typing import BinaryIO, List

MAX_DEPTH = 16
MAX_ENTRIES = 10000
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class Collector:
    def __init__(
        self, out: BinaryIO, patterns: List[str], max_files: int, max_total: int, max_file: int
    ) -> None:
        self.out = out
        self.patterns = patterns
        self.max_files = max_files
        self.max_total = max_total
        self.max_file = max_file
        self.files = 0
        self.total = 0
        self.entries = 0
        self.uid = os.getuid()

    def emit(self, header: dict, payload: bytes = b"") -> None:
        self.out.write(json.dumps(header, separators=(",", ":")).encode("utf-8") + b"\n")
        if payload:
            self.out.write(payload)
        self.out.flush()

    def matches(self, rel: str) -> bool:
        return any(fnmatch.fnmatchcase(rel, p) for p in self.patterns)

    def walk(self, dir_fd: int, prefix: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            self.emit({"skip": prefix, "reason": "too_deep"})
            return
        with os.scandir(dir_fd) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            self.entries += 1
            if self.entries > MAX_ENTRIES:
                self.emit({"skip": prefix, "reason": "too_many_entries"})
                return
            rel = f"{prefix}{entry.name}"
            if entry.is_symlink():
                if self.matches(rel):
                    self.emit({"skip": rel, "reason": "symlink"})
                continue
            if entry.is_dir(follow_symlinks=False):
                try:
                    fd = os.open(entry.name, _DIR_FLAGS, dir_fd=dir_fd)
                except OSError:
                    continue  # replaced with something else, or unreadable
                try:
                    self.walk(fd, rel + "/", depth + 1)
                finally:
                    os.close(fd)
                continue
            if self.matches(rel):
                self.collect_file(dir_fd, entry.name, rel)

    def collect_file(self, dir_fd: int, name: str, rel: str) -> None:
        if self.files >= self.max_files:
            self.emit({"skip": rel, "reason": "too_many_files"})
            return
        try:
            fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
        except OSError as exc:  # ELOOP for a symlink swapped in after scandir
            self.emit({"skip": rel, "reason": f"open_failed:{exc.errno}"})
            return
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                self.emit({"skip": rel, "reason": "not_regular"})
                return
            if st.st_nlink != 1:
                self.emit({"skip": rel, "reason": "hardlink"})
                return
            if st.st_uid != self.uid:
                self.emit({"skip": rel, "reason": "foreign_owner"})
                return
            budget = min(self.max_file, self.max_total - self.total)
            if budget <= 0:
                self.emit({"skip": rel, "reason": "total_limit"})
                return
            data = _read_up_to(fd, budget)
            truncated = st.st_size > len(data) or len(data) == budget and _more(fd)
            self.files += 1
            self.total += len(data)
            self.emit({"file": rel, "size": len(data), "truncated": bool(truncated)}, data)
        finally:
            os.close(fd)


def _read_up_to(fd: int, limit: int) -> bytes:
    chunks, got = [], 0
    while got < limit:
        chunk = os.read(fd, min(1 << 20, limit - got))
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def _more(fd: int) -> bool:
    return bool(os.read(fd, 1))


def main(argv: List[str]) -> int:
    workdir, plan_json = argv[1], argv[2]
    plan = json.loads(plan_json)
    out = sys.stdout.buffer
    collector = Collector(
        out, plan["patterns"], plan["max_files"], plan["max_total_bytes"], plan["max_file_bytes"]
    )
    root_fd = os.open(workdir, _DIR_FLAGS)
    try:
        collector.walk(root_fd, "", 0)
    finally:
        os.close(root_fd)
    collector.emit({"end": True})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
