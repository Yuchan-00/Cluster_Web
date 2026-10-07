import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from cluster_common.canonical_json import CanonicalError, canonical_dumps, hash_value  # noqa: E402
from cluster_common.redact import PATTERNS, RedactingFilter, Redactor, redact  # noqa: E402


def test_patterns_mask_known_formats():
    text = (
        "token cat_"
        + "A" * 43
        + " and cst_"
        + "b" * 43
        + " key sk-ant-api03-"
        + "x" * 40
        + " bot 123456789:"
        + "Z" * 35
        + " Authorization: Bearer abc.def"
    )
    out = redact(text)
    for kind in ("agent-token", "service-token", "anthropic-key", "telegram-token", "bearer"):
        assert f"[REDACTED:{kind}]" in out, kind
    assert "A" * 43 not in out and "x" * 40 not in out


def test_credential_assignments_keep_key_mask_value():
    assert redact("password=hunter22 ok") == "password=[REDACTED:credential] ok"
    assert redact('API_KEY: "abcd1234"') == "API_KEY: [REDACTED:credential]"
    assert redact("token=abc") == "token=abc"  # too short to be a secret, left alone


def test_private_key_block():
    pem = "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEE\n-----END EC PRIVATE KEY-----"
    assert redact(f"x {pem} y") == "x [REDACTED:private-key] y"


def test_exact_values_and_objects():
    r = Redactor(PATTERNS)
    r.register("my-very-secret-value", "bot-token")
    r.register("short", "x")  # ignored: too short
    out = r.redact_obj({"a": ["my-very-secret-value here", 1], "b": {"c": "short"}})
    assert out == {"a": ["[REDACTED:bot-token] here", 1], "b": {"c": "short"}}


def test_logging_filter():
    r = Redactor()
    r.register("supersecrettoken", "t")
    logger = logging.getLogger("redact-test")
    handler = logging.Handler()
    seen = []
    handler.emit = lambda rec: seen.append(rec.getMessage())  # type: ignore[assignment]
    handler.addFilter(RedactingFilter(r))
    logger.addHandler(handler)
    logger.warning("value %s and %s", "supersecrettoken", "cat_" + "Q" * 43)
    assert seen == ["value [REDACTED:t] and [REDACTED:agent-token]"]


def test_canonical_json():
    assert canonical_dumps({"b": 1, "a": [True, None, "é"]}) == '{"a":[true,null,"é"],"b":1}'
    assert hash_value({"a": 1, "b": 2}) == hash_value({"b": 2, "a": 1})
    with pytest.raises(CanonicalError):
        canonical_dumps({"ts": 1.5})
    with pytest.raises(CanonicalError):
        canonical_dumps({1: "x"})
    with pytest.raises(CanonicalError):
        canonical_dumps({"x": object()})
