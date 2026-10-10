"""Settings from the environment, with an optional ``.env`` file.

The ``.env`` loader is deliberately tiny (KEY=VALUE lines, optional quotes, #
comments) and never overrides a variable that is already set in the shell.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_SAMPLE_PATH = "nl2sql_sample.db"
DEFAULT_DB = f"sqlite:{DEFAULT_SAMPLE_PATH}"
DEFAULT_SEMANTIC_FILE = "semantic.yaml"


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    p = Path(path)
    loaded: dict[str, str] = {}
    if not p.is_file():
        return loaded
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def default_db() -> str:
    return os.environ.get("NL2SQL_DB") or DEFAULT_DB


def default_provider() -> str:
    return os.environ.get("NL2SQL_PROVIDER") or "stub"


def default_semantic() -> str | None:
    """``NL2SQL_SEMANTIC``, else ./semantic.yaml if it exists, else None (built-in policy)."""
    env = os.environ.get("NL2SQL_SEMANTIC")
    if env:
        return env
    if Path(DEFAULT_SEMANTIC_FILE).is_file():
        return DEFAULT_SEMANTIC_FILE
    return None
