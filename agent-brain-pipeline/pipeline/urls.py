from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class RepositoryURL:
    original: str
    normalized: str
    host: str
    platform: str
    namespace: str
    name: str

    @property
    def source_id(self) -> str:
        return f"{self.platform}-{self.namespace.replace('/', '-')}-{self.name}".lower()


def parse_repository_url(value: str) -> RepositoryURL:
    raw = value.strip()
    if not raw:
        raise ValueError("Repository URL is empty")

    if re.match(r"^(github\.com|gitlab\.com)/", raw, flags=re.I):
        raw = "https://" + raw
    if raw.startswith("git@"):
        match = re.match(r"git@(?P<host>[^:]+):(?P<path>.+)$", raw)
        if not match:
            raise ValueError("Invalid SSH repository URL")
        raw = f"https://{match.group('host')}/{match.group('path')}"

    parsed = urlparse(raw)
    host = parsed.hostname.lower() if parsed.hostname else ""
    if host not in {"github.com", "gitlab.com"}:
        raise ValueError("Only github.com and gitlab.com repositories are supported in v0.1")

    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) < 2:
        raise ValueError("Repository URL must contain owner/group and repository name")
    name = parts[-1]
    if name.endswith(".git"):
        name = name[:-4]
    namespace = "/".join(parts[:-1])
    platform = "github" if host == "github.com" else "gitlab"
    normalized = f"https://{host}/{namespace}/{name}.git"
    return RepositoryURL(value, normalized, host, platform, namespace, name)
