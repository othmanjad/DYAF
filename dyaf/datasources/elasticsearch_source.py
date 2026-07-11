"""Elasticsearch datasource adapter.

Discovers fields dynamically from the index mapping (``GET <index>/_mapping``)
so any newly indexed field is immediately available to the Rule Builder —
no field names are hardcoded. Query execution pushes the compiled
Elasticsearch DSL (see dyaf.rules.query_builder) down to the cluster.

This adapter is optional: the reference deployment runs on SQLite, and the
engine only depends on the DataSource interface.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

import requests

from .base import DataSource, FieldInfo


class ElasticsearchDataSource(DataSource):
    def __init__(self, base_url: str, index: str, name: Optional[str] = None,
                 timestamp_field: str = "executed_at", session: Optional[requests.Session] = None):
        self.base_url = base_url.rstrip("/")
        self.index = index
        self.name = name or index
        self.timestamp_field = timestamp_field
        self.http = session or requests.Session()

    # ------------------------------------------------------------------
    def get_fields(self) -> list[FieldInfo]:
        resp = self.http.get(f"{self.base_url}/{self.index}/_mapping", timeout=10)
        resp.raise_for_status()
        mapping = resp.json()
        props = mapping.get(self.index, {}).get("mappings", {}).get("properties", {})
        fields: list[FieldInfo] = []
        self._walk_properties(props, "", fields)
        return fields

    def _walk_properties(self, props: dict, prefix: str, out: list[FieldInfo]) -> None:
        for name, spec in props.items():
            full = f"{prefix}{name}"
            if "properties" in spec:  # object / nested — recurse
                self._walk_properties(spec["properties"], f"{full}.", out)
            else:
                out.append(FieldInfo(name=full, type=spec.get("type", "keyword")))

    # ------------------------------------------------------------------
    def fetch(self, start: Optional[datetime] = None, end: Optional[datetime] = None,
              condition: Optional[dict] = None) -> list[dict]:
        from ..rules.query_builder import build_bool_query

        must: list[dict] = []
        if condition:
            must.append(build_bool_query(condition))
        if self.timestamp_field and (start or end):
            rng: dict = {}
            if start:
                rng["gte"] = start.isoformat()
            if end:
                rng["lte"] = end.isoformat()
            must.append({"range": {self.timestamp_field: rng}})

        body = {"query": {"bool": {"must": must}} if must else {"match_all": {}}, "size": 10000}
        resp = self.http.post(f"{self.base_url}/{self.index}/_search", json=body, timeout=30)
        resp.raise_for_status()
        hits = resp.json().get("hits", {}).get("hits", [])
        return [h["_source"] for h in hits]
