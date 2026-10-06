import os
import sys
from typing import Dict, Optional, Sequence

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cluster_agent.collectors.base import SysFS  # noqa: E402


@pytest.fixture
def make_sysfs(tmp_path):
    """Build a fake filesystem tree from {"/abs/path": "content"} and return a SysFS on it."""

    def build(files: Dict[str, str]) -> SysFS:
        for path, content in files.items():
            full = tmp_path / path.lstrip("/")
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
        return SysFS(str(tmp_path))

    return build


class FakeRunner:
    """Stands in for run_command: maps argv tuples to canned stdout and records calls."""

    def __init__(self, outputs: Optional[Dict[tuple, Optional[str]]] = None) -> None:
        self.outputs = outputs or {}
        self.calls = []

    def __call__(self, argv: Sequence[str], timeout: float) -> Optional[str]:
        self.calls.append(tuple(argv))
        return self.outputs.get(tuple(argv))


@pytest.fixture
def fake_runner():
    return FakeRunner
