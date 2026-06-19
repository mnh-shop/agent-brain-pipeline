from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

SECRET_PATTERNS = [
    re.compile(r"sk-or-v1-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)(token|api[_-]?key|password)=([^\s&]+)"),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def redact(text: str) -> str:
    result = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            result = pattern.sub(lambda m: f"{m.group(1)}=***REDACTED***", result)
        else:
            result = pattern.sub("***REDACTED***", result)
    return result


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str


def run_command(
    args: Iterable[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 3600,
    check: bool = True,
) -> CommandResult:
    command = [str(x) for x in args]
    merged_env = os.environ.copy()
    if env:
        merged_env.update({str(k): str(v) for k, v in env.items()})
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        timeout=timeout,
        text=True,
        capture_output=True,
        check=False,
    )
    result = CommandResult(command, completed.returncode, redact(completed.stdout), redact(completed.stderr))
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    return result


def ignored(path: Path, root: Path, ignored_directories: list[str], ignored_globs: list[str]) -> bool:
    relative = path.relative_to(root)
    if any(part in ignored_directories for part in relative.parts):
        return True
    return any(fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(str(relative), pattern) for pattern in ignored_globs)


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.").lower()
    return slug or "unknown"


def analysis_workspace(run: dict[str, Any], component: str) -> Path:
    from pipeline.config import data_dir
    return data_dir() / "derived" / run["source_id"] / run["commit_sha"] / "workspaces" / component / "repository"


def component_dir(run: dict[str, Any], component: str) -> Path:
    from pipeline.config import data_dir
    return data_dir() / "derived" / run["source_id"] / run["commit_sha"] / component


def ensure_analysis_workspace(run: dict[str, Any], component: str) -> Path:
    source = Path(run["snapshot_path"])
    destination = analysis_workspace(run, component)
    marker = destination.parent / ".source-sha256"
    expected = f"{run['source_id']}:{run['commit_sha']}"
    if destination.exists() and marker.exists() and marker.read_text(encoding="utf-8").strip() == expected:
        return destination
    if destination.parent.exists():
        shutil.rmtree(destination.parent)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True)
    marker.write_text(expected + "\n", encoding="utf-8")
    return destination


def temporary_askpass(username: str, password: str):
    class AskPassContext:
        def __enter__(self):
            self.tempdir = tempfile.TemporaryDirectory(prefix="agent-brain-askpass-")
            path = Path(self.tempdir.name) / "askpass.sh"
            path.write_text(
                "#!/bin/sh\n"
                "case \"$1\" in\n"
                "  *Username*) printf '%s\\n' \"$GIT_AUTH_USERNAME\" ;;\n"
                "  *) printf '%s\\n' \"$GIT_AUTH_PASSWORD\" ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            path.chmod(0o700)
            self.env = {
                "GIT_ASKPASS": str(path),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_AUTH_USERNAME": username,
                "GIT_AUTH_PASSWORD": password,
            }
            return self.env

        def __exit__(self, exc_type, exc, tb):
            self.tempdir.cleanup()

    return AskPassContext()
