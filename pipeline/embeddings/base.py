from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class EmbeddingBackendInfo:
    provider: str
    model: str
    revision: str | None
    dimensions: int
    normalize: bool
    artifact_hashes: tuple[str, ...] = ()
    cache_path: str | None = None


class EmbeddingBackend(Protocol):
    @property
    def info(self) -> EmbeddingBackendInfo: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
