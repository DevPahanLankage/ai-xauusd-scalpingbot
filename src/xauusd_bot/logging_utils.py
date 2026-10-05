from __future__ import annotations

import logging
import re
from collections.abc import Iterable


class RedactingFormatter(logging.Formatter):
    def __init__(self, *args: object, secrets: Iterable[str] = (), **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._secrets = tuple(secret for secret in secrets if secret)

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        for secret in self._secrets:
            rendered = rendered.replace(secret, "[REDACTED]")
        rendered = re.sub(
            r"(?i)authorization\s*[:=]\s*[^,}\r\n]+",
            "[REDACTED AUTH HEADER]",
            rendered,
        )
        rendered = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", rendered)
        return rendered


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s",
            secrets=secrets,
        )
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))

    # HTTP libraries can be very noisy. Keeping them above INFO also prevents
    # accidental wire-level logging of request metadata.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("mcp.client.streamable_http").setLevel(logging.ERROR)
