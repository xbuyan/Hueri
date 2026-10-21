"""Settings for the demo backend. Secrets come from the environment or the 0600 env file
outside the repo; values are never logged or returned by any endpoint."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

DEFAULT_URL = "http://127.0.0.1:8765/mcp"
DEFAULT_ENV_FILE = Path.home() / ".config" / "hueri-mcp" / "demo.env"


def _read_env_file(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def _find_token(values: Dict[str, str]) -> Optional[str]:
    for exact in ("DEMO_MCP_TOKEN", "MCP_BEARER_TOKEN", "MCP_TOKEN", "MCP_AUTH_TOKEN"):
        if values.get(exact):
            return values[exact]
    cands = [k for k in values if "TOKEN" in k.upper() and values[k]]
    return values[cands[0]] if len(cands) == 1 else None


@dataclass(frozen=True)
class DemoSettings:
    mcp_url: str
    token: Optional[str]
    include_mock: bool
    token_source: str  # "env", "file" or "missing" (a label only, never a value)

    @classmethod
    def load(cls, env: Optional[Dict[str, str]] = None, env_file: Optional[Path] = None) -> "DemoSettings":
        env = dict(os.environ) if env is None else env
        file_vals = _read_env_file(env_file or Path(env.get("DEMO_ENV_FILE", DEFAULT_ENV_FILE)))
        token = env.get("DEMO_MCP_TOKEN") or None
        source = "env" if token else "missing"
        if not token:
            token = _find_token(file_vals)
            source = "file" if token else "missing"
        return cls(
            mcp_url=env.get("DEMO_MCP_URL", DEFAULT_URL),
            token=token,
            include_mock=env.get("DEMO_INCLUDE_MOCK", "true").lower() not in ("0", "false", "no"),
            token_source=source,
        )
