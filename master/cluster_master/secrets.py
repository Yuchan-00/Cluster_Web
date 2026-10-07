"""Tokens and credentials (docs/design/security.md 8.2, 12).

Node tokens are `cat_` + 32 random bytes (base64url, 43 chars); service tokens use `cst_`.
Only SHA-256 hashes are stored: the tokens have 256 bits of entropy, so a slow hash buys
nothing, and comparison is constant-time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import stat

TOKEN_BODY = re.compile(r"[A-Za-z0-9_-]{43}")
NODE_PREFIX = "cat_"
SERVICE_PREFIX = "cst_"


def new_token(prefix: str) -> str:
    body = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    return prefix + body


def is_token(value: str, prefix: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith(prefix)
        and TOKEN_BODY.fullmatch(value[len(prefix) :]) is not None
    )


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def token_matches(presented: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(presented), stored_hash)


class CredentialError(RuntimeError):
    pass


def load_credential(name: str, credentials_dir: str, required: bool = True) -> str | None:
    """Read a secret from systemd's LoadCredential directory (or the configured fallback dir).

    The file must not be readable by group or others; a world-readable secret is refused
    rather than silently used.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
        raise CredentialError(f"invalid credential name {name!r}")
    path = os.path.join(credentials_dir, name)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        if required:
            raise CredentialError(f"credential {name} not found in {credentials_dir}") from None
        return None
    except OSError as exc:
        raise CredentialError(f"cannot open credential {name}: {exc}") from exc
    with os.fdopen(fd, encoding="utf-8") as f:
        st = os.fstat(f.fileno())
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO) and not _is_credentials_dir(credentials_dir):
            raise CredentialError(f"credential {name} is readable by group/others (chmod 600)")
        value = f.read().strip()
    if not value:
        raise CredentialError(f"credential {name} is empty")
    return value


def _is_credentials_dir(path: str) -> bool:
    # systemd mounts $CREDENTIALS_DIRECTORY read-only for the service only; its file modes
    # reflect that already, so the per-file check is only for the plain-directory fallback.
    return os.environ.get("CREDENTIALS_DIRECTORY") == path


def generate_or_load_key(name: str, credentials_dir: str, dev_state_dir: str | None) -> bytes:
    """Load a 32-byte key credential; in development, create it under the state dir once."""
    value = load_credential(name, credentials_dir, required=dev_state_dir is None)
    if value is not None:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if len(raw) != 32:
            raise CredentialError(f"credential {name} must be 32 bytes base64url")
        return raw
    if dev_state_dir is None:  # pragma: no cover - load_credential(required=True) raised already
        raise CredentialError(f"credential {name} not found")
    os.makedirs(dev_state_dir, mode=0o700, exist_ok=True)
    path = os.path.join(dev_state_dir, name)
    try:
        with open(path, encoding="ascii") as f:
            value = f.read().strip()
    except FileNotFoundError:
        raw = secrets.token_bytes(32)
        value = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(value + "\n")
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
