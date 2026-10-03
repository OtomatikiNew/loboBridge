"""Logging setup + secret redaction.

logging has no pino-style redaction like the Node side, so redact() is the
manual equivalent: wrap anything that might carry srlobo_token or mqtt
creds before logging it. No filter catches this automatically, so don't
log a raw dict without running it through redact() first.
"""

import logging
from typing import Any

SENSITIVE_KEYS = {
    "srlobo_token",
    "token",
    "password",
    "mqtt_password",
    "client_key",
    "client_cert",
    "ca_cert",
}

REDACTED = "[REDACTED]"


def redact(value: Any) -> Any:
    """Recursively masks any dict value whose key is in SENSITIVE_KEYS.

    Safe on anything, non-dict/list values just pass through. Run bootstrap
    responses, options, mqtt config etc through this before logging them.

    Args:
        value: Value to redact; typically a dict, list, or scalar.

    Returns:
        A copy of value with sensitive dict values replaced by "[REDACTED]".
    """
    if isinstance(value, dict):
        return {
            key: (REDACTED if key.lower() in SENSITIVE_KEYS else redact(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def setup_logging(level: str) -> None:
    """Configures the root logger's level and output format.

    Args:
        level: Log level name (case-insensitive), e.g. "info" or "debug".
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
