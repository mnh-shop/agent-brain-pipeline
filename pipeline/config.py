from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@lru_cache(maxsize=1)
def get_config() -> dict[str, Any]:
    path = Path(os.environ.get("AGENT_BRAIN_CONFIG", "/app/config/runtime.yaml"))
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid configuration at {path}")
    return data


def data_dir() -> Path:
    path = Path(get_config()["storage"]["data_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path


def obsidian_dir() -> Path:
    path = Path(get_config()["storage"]["obsidian_dir"])
    path.mkdir(parents=True, exist_ok=True)
    return path
