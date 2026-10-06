"""Transport to the master: TLS pinned to the internal CA, token in the upgrade request,
reconnect backoff, and a websockets compatibility layer (10.x on Python 3.8 up to current).

docs/design/security.md 8.1-8.3.
"""

from __future__ import annotations

import inspect
import os
import random
import re
import ssl
import stat
from typing import Any, Dict, Optional

try:  # websockets >= 13: new asyncio implementation
    from websockets.asyncio.client import connect as _ws_connect

    _HEADERS_KW = "additional_headers"
except ImportError:  # websockets 10-12 (Debian/Ubuntu apt packages)
    from websockets.legacy.client import connect as _ws_connect  # type: ignore[no-redef]

    _HEADERS_KW = "extra_headers"

from websockets.exceptions import ConnectionClosed, InvalidHandshake

from .config import ConfigError

TOKEN_RE = re.compile(r"cat_[A-Za-z0-9_-]{43}")
MAX_MESSAGE = 1024 * 1024  # security.md 8.3

CLOSE_AUTH = 4401
CLOSE_IDENTITY = 4403
CLOSE_DUPLICATE = 4409
CLOSE_RATE = 4429


def make_ssl_context(ca_file: str) -> ssl.SSLContext:
    """Trust only the cluster's internal CA (never the system store) and check the hostname."""
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_file)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def load_token(path: str) -> str:
    """Read the node token, refusing files other accounts could read (like ssh does for keys)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ConfigError(f"cannot open token file {path}: {exc}") from exc
    with os.fdopen(fd, encoding="ascii", errors="replace") as f:
        st = os.fstat(f.fileno())
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError(f"{path} must not be accessible by group or others (chmod 600)")
        if st.st_uid != os.getuid():
            raise ConfigError(f"{path} must be owned by the agent account")
        token = f.read().strip()
    if not TOKEN_RE.fullmatch(token):
        raise ConfigError(f"{path} does not contain a node token (cat_...)")
    return token


def connect(url: str, token: str, node_id: str, ssl_ctx: Optional[ssl.SSLContext]) -> Any:
    """websockets.connect with the auth headers, sane limits and no ambient proxy."""
    kwargs: Dict[str, Any] = {
        _HEADERS_KW: {"Authorization": f"Bearer {token}", "X-Node-Id": node_id},
        "max_size": MAX_MESSAGE,
        "ping_interval": 20,
        "ping_timeout": 20,
        "close_timeout": 5,
        "open_timeout": 10,
    }
    if url.startswith("wss://"):
        kwargs["ssl"] = ssl_ctx
    params = inspect.signature(_ws_connect).parameters
    if "proxy" in params:  # websockets >= 15 would otherwise honour HTTPS_PROXY
        kwargs["proxy"] = None
    return _ws_connect(url, **kwargs)


def close_code(exc: BaseException) -> Optional[int]:
    if isinstance(exc, ConnectionClosed):
        rcvd = getattr(exc, "rcvd", None)
        if rcvd is not None:
            return rcvd.code
        return getattr(exc, "code", None)
    return None


def handshake_status(exc: BaseException) -> Optional[int]:
    """HTTP status of a refused upgrade (401 = bad token), across websockets versions."""
    if not isinstance(exc, InvalidHandshake):
        return None
    response = getattr(exc, "response", None)
    if response is not None and hasattr(response, "status_code"):
        return int(response.status_code)
    status = getattr(exc, "status_code", None)
    return int(status) if status is not None else None


class Backoff:
    """1, 2, 4 ... 30 seconds with +-20% jitter so five nodes do not reconnect in lockstep."""

    def __init__(self, base: float = 1.0, cap: float = 30.0) -> None:
        self.base = base
        self.cap = cap
        self.attempt = 0

    def reset(self) -> None:
        self.attempt = 0

    def next(self) -> float:
        delay = min(self.cap, self.base * (2**self.attempt))
        self.attempt += 1
        return delay * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not crypto
