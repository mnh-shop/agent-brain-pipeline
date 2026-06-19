from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CanonicalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
