"""Environment-driven settings for the read-only MCP service.

Standard library only. This module must never import app.config, app.db or any
other part of the application's startup stack (see test_mcp_security.py).

Environment variables
---------------------
MCP_BEARER_TOKEN      required. 32+ printable ASCII characters, no whitespace.
MCP_ALLOWED_HOSTS     required unless MCP_DEV_MODE=1. Comma-separated exact Host
                      header values, including the port when it is not the
                      scheme default (e.g. "mcp.example.org,127.0.0.1:8765").
MCP_ALLOWED_ORIGINS   optional. Comma-separated exact origins
                      ("https://app.example.org"). Empty means: reject every
                      request that carries an Origin header (non-browser
                      clients send none, so they are unaffected).
MCP_DEV_MODE          "1" enables local-development defaults for the host list.
                      Anything else is treated as production (fail closed).
MCP_PORT              optional, default 8765. Only used for dev-mode defaults.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

MIN_TOKEN_LENGTH = 32
DEFAULT_PORT = 8765

_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")  # printable ASCII, no whitespace


class ConfigError(RuntimeError):
    """Raised at startup when the security configuration is missing or unsafe."""


def _split(raw: str) -> Tuple[str, ...]:
    items = []
    for part in raw.split(","):
        part = part.strip().lower()
        if part:
            items.append(part)
    return tuple(items)


@dataclass(frozen=True)
class McpSettings:
    bearer_token: str = field(repr=False)
    allowed_hosts: Tuple[str, ...]
    allowed_origins: Tuple[str, ...]
    dev_mode: bool = False
    port: int = DEFAULT_PORT

    @classmethod
    def from_environ(cls, environ: Optional[Mapping[str, str]] = None) -> "McpSettings":
        env = os.environ if environ is None else environ

        dev_mode = env.get("MCP_DEV_MODE", "") == "1"

        token = env.get("MCP_BEARER_TOKEN", "")
        if not token:
            raise ConfigError("MCP_BEARER_TOKEN is required")
        if len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(
                f"MCP_BEARER_TOKEN must be at least {MIN_TOKEN_LENGTH} characters"
            )
        if not _TOKEN_RE.match(token):
            raise ConfigError(
                "MCP_BEARER_TOKEN must be printable ASCII without whitespace"
            )

        try:
            port = int(env.get("MCP_PORT", "") or DEFAULT_PORT)
        except ValueError:
            raise ConfigError("MCP_PORT must be an integer") from None
        if not 1 <= port <= 65535:
            raise ConfigError("MCP_PORT must be between 1 and 65535")

        hosts = _split(env.get("MCP_ALLOWED_HOSTS", ""))
        if not hosts:
            if not dev_mode:
                raise ConfigError(
                    "MCP_ALLOWED_HOSTS is required (set MCP_DEV_MODE=1 only for "
                    "local development)"
                )
            hosts = (f"localhost:{port}", f"127.0.0.1:{port}")
        if any("*" in h for h in hosts):
            raise ConfigError("wildcards are not allowed in MCP_ALLOWED_HOSTS")

        origins = tuple(o.rstrip("/") for o in _split(env.get("MCP_ALLOWED_ORIGINS", "")))
        for origin in origins:
            if "*" in origin:
                raise ConfigError("wildcards are not allowed in MCP_ALLOWED_ORIGINS")
            if not origin.startswith(("http://", "https://")):
                raise ConfigError(
                    "each MCP_ALLOWED_ORIGINS entry must start with http:// or https://"
                )

        return cls(
            bearer_token=token,
            allowed_hosts=hosts,
            allowed_origins=origins,
            dev_mode=dev_mode,
            port=port,
        )
