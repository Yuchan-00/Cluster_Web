"""Building blocks shared by every collector: the interface, sysfs access and command running."""

from __future__ import annotations

import glob as _glob
import logging
import os
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

# Runs a short diagnostic command and returns its stdout, or None if it could not run.
CommandRunner = Callable[[Sequence[str], float], Optional[str]]


class Collector:
    """One group of metrics. Every method must be cheap and must not raise."""

    name = "base"

    def static_info(self) -> Dict[str, Any]:
        """Facts that do not change while the agent runs (sent once per connection)."""
        return {}

    def collect(self) -> Dict[str, Any]:
        """Metrics sampled every interval. Board collectors put their own keys under "extra"."""
        return {}

    def collect_slow(self) -> Dict[str, Any]:
        """More expensive metrics, sampled every few intervals."""
        return {}


def safe(fn: Callable[[], Any], default: Any = None, what: str = "") -> Any:
    """Return fn() or default. One failing metric must never drop the whole sample."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - metrics are best effort by design
        log.debug("metric %s failed: %s", what or getattr(fn, "__name__", "?"), exc)
        return default


class SysFS:
    """Reads files under a root directory so tests can point it at a fixture tree."""

    def __init__(self, root: str = "/") -> None:
        self.root = root

    def path(self, p: str) -> str:
        return os.path.join(self.root, p.lstrip("/"))

    def read(self, p: str) -> Optional[str]:
        try:
            with open(self.path(p), encoding="utf-8", errors="replace") as f:
                # device-tree strings are NUL-terminated
                return f.read().replace("\x00", "").strip()
        except OSError:
            return None

    def read_int(self, p: str) -> Optional[int]:
        text = self.read(p)
        if text is None:
            return None
        try:
            return int(text.split()[0], 0)
        except (ValueError, IndexError):
            return None

    def exists(self, p: str) -> bool:
        return os.path.exists(self.path(p))

    def glob(self, pattern: str) -> List[str]:
        """Matching paths, returned relative to root (with a leading slash) and sorted."""
        root = os.path.abspath(self.root)
        matches = _glob.glob(self.path(pattern))
        return sorted("/" + os.path.relpath(m, root) for m in matches)


def run_command(argv: Sequence[str], timeout: float) -> Optional[str]:
    """Run a diagnostic command. A tool that keeps printing until killed still yields its output."""
    try:
        proc = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or b""
        return out.decode("utf-8", "replace") if isinstance(out, bytes) else out
    except OSError as exc:
        log.debug("cannot run %s: %s", argv[0], exc)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")
