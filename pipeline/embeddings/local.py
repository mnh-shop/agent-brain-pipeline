from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from pipeline.embeddings.base import EmbeddingBackendInfo

TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]+", re.UNICODE)


@dataclass
class LocalDeterministicEmbeddingBackend:
    model: str = "agent-brain-local-hash-embedding"
    revision: str = "v1"
    dimensions: int = 384
    normalize: bool = True
    cache_path: Path | None = None
    artifact_hashes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def info(self) -> EmbeddingBackendInfo:
        return EmbeddingBackendInfo(
            provider="local",
            model=self.model,
            revision=self.revision,
            dimensions=self.dimensions,
            normalize=self.normalize,
            artifact_hashes=self.artifact_hashes,
            cache_path=str(self.cache_path) if self.cache_path else None,
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = TOKEN_RE.findall(text.lower()) or [text.lower()]
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            primary = int.from_bytes(digest[0:4], "big") % self.dimensions
            secondary = int.from_bytes(digest[4:8], "big") % self.dimensions
            weight = 1.0 + (len(token) % 7) / 10.0
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[primary] += sign * weight
            vector[secondary] += (1.0 if digest[9] & 1 else -1.0) * (weight / 2.0)
        if self.normalize:
            norm = math.sqrt(sum(value * value for value in vector))
            if norm:
                vector = [value / norm for value in vector]
        return vector
