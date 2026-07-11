"""Ingest adapter base + registry.

Every adapter turns one source into a list of ``RawAsset`` records (the
pre-normalization, source-specific shape). Adapters pick "online" mode (real
API, when credentials are configured) or "offline" mode (bundled fixture).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

from ..models import RawAsset


@dataclass
class IngestResult:
    assets: List[RawAsset]
    mode: str          # "online" | "fixture"
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


class Adapter:
    """Subclasses implement ``fetch``."""
    source: str = ""

    def fetch(self, settings) -> IngestResult:  # noqa: ANN001
        raise NotImplementedError


_REGISTRY: Dict[str, Callable[[], Adapter]] = {}


def register(cls: type) -> type:
    _REGISTRY[cls.source] = cls
    return cls


def get_adapter(source: str) -> Adapter:
    if source not in _REGISTRY:
        raise KeyError(f"unknown source {source!r}; known: {list(_REGISTRY)}")
    return _REGISTRY[source]()


def all_sources() -> List[str]:
    return sorted(_REGISTRY)