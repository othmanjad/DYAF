"""Datasource abstraction.

The rule engine never talks to a storage backend directly — it goes through
a DataSource. Each datasource exposes:

* dynamic field discovery (``get_fields``) — fields are introspected from
  the backend (Elasticsearch mapping / SQL schema), never hardcoded, so any
  newly indexed field automatically becomes available in the Rule Builder.
* row fetching for a time window + condition tree (``fetch``).

Backends implemented: SQLite (reference/local) and Elasticsearch (adapter).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional


@dataclass
class FieldInfo:
    name: str
    type: str  # keyword | text | long | double | date | boolean

    def to_dict(self) -> dict:
        return asdict(self)


class DataSource(ABC):
    name: str
    timestamp_field: Optional[str] = None  # None => datasource has no event time

    @abstractmethod
    def get_fields(self) -> list[FieldInfo]:
        """Discover available fields dynamically from the backend."""

    @abstractmethod
    def fetch(self, start: Optional[datetime] = None, end: Optional[datetime] = None,
              condition: Optional[dict] = None) -> list[dict]:
        """Return rows within [start, end] matching the condition tree."""

    def field_names(self) -> set[str]:
        return {f.name for f in self.get_fields()}


class DataSourceRegistry:
    """Pluggable registry — new datasources register without engine changes."""

    def __init__(self):
        self._sources: dict[str, DataSource] = {}

    def register(self, source: DataSource) -> None:
        self._sources[source.name] = source

    def unregister(self, name: str) -> None:
        self._sources.pop(name, None)

    def get(self, name: str) -> DataSource:
        if name not in self._sources:
            raise KeyError(f"Unknown datasource: {name}")
        return self._sources[name]

    def names(self) -> list[str]:
        return sorted(self._sources)
