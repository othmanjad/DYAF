"""Elasticsearch datasource adapter.

The platform's detection layer always reads from Elasticsearch. This
adapter:

* discovers fields dynamically from the index mapping (``GET <index>/_mapping``)
  so any newly indexed field is immediately available to the Rule Builder —
  no field names are hardcoded (requirement §9);
* bootstraps the index on first use (``ensure_index`` creates it with the
  platform mapping when missing);
* bulk-indexes documents (used by seeding and CSV upload);
* executes rule fetches by compiling the condition tree to Elasticsearch
  DSL (see dyaf.rules.query_builder) and pushing it down to the cluster.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import requests

from .base import DataSource, FieldInfo


class ElasticsearchError(RuntimeError):
    pass


class ElasticsearchDataSource(DataSource):
    def __init__(self, base_url: str, index: str, name: Optional[str] = None,
                 timestamp_field: Optional[str] = "executed_at",
                 mappings: Optional[dict] = None,
                 session: Optional[requests.Session] = None):
        self.base_url = base_url.rstrip("/")
        self.index = index
        self.name = name or index
        self.timestamp_field = timestamp_field
        self.mappings = mappings or {"dynamic": True, "properties": {}}
        self.http = session or requests.Session()
        # local cluster access must never go through the outbound proxy
        self.http.trust_env = False if "localhost" in base_url or "127.0.0.1" in base_url else self.http.trust_env

    # ------------------------------------------------------------------
    # Cluster / index administration
    # ------------------------------------------------------------------
    def ping(self) -> bool:
        try:
            return self.http.get(self.base_url, timeout=5).ok
        except requests.RequestException:
            return False

    def index_exists(self) -> bool:
        resp = self.http.head(f"{self.base_url}/{self.index}", timeout=10)
        return resp.status_code == 200

    def create_index(self) -> dict:
        resp = self.http.put(
            f"{self.base_url}/{self.index}",
            json={"settings": {"number_of_shards": 1, "number_of_replicas": 0},
                  "mappings": self.mappings},
            timeout=30,
        )
        if not resp.ok:
            raise ElasticsearchError(f"Failed to create index '{self.index}': {resp.text}")
        return resp.json()

    def ensure_index(self) -> dict:
        """Create the index with the platform mapping on first run."""
        if self.index_exists():
            return {"index": self.index, "created": False}
        self.create_index()
        return {"index": self.index, "created": True}

    def refresh(self) -> None:
        self.http.post(f"{self.base_url}/{self.index}/_refresh", timeout=10)

    def count(self) -> int:
        resp = self.http.get(f"{self.base_url}/{self.index}/_count", timeout=10)
        return resp.json().get("count", 0) if resp.ok else 0

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------
    def bulk_index(self, docs: list[dict], id_field: Optional[str] = None,
                   refresh: bool = True) -> dict:
        """Bulk-index documents; returns {indexed, errors}."""
        if not docs:
            return {"indexed": 0, "errors": []}
        lines = []
        for doc in docs:
            action: dict = {"index": {"_index": self.index}}
            if id_field and doc.get(id_field) is not None:
                action["index"]["_id"] = str(doc[id_field])
            lines.append(json.dumps(action))
            lines.append(json.dumps(doc, default=str))
        body = "\n".join(lines) + "\n"
        resp = self.http.post(f"{self.base_url}/_bulk", data=body.encode("utf-8"),
                              headers={"Content-Type": "application/x-ndjson"}, timeout=60)
        if not resp.ok:
            raise ElasticsearchError(f"Bulk indexing failed: {resp.text}")
        payload = resp.json()
        errors = []
        for item in payload.get("items", []):
            info = item.get("index", {})
            if info.get("error"):
                errors.append({"id": info.get("_id"), "error": info["error"]})
        if refresh:
            self.refresh()
        return {"indexed": len(docs) - len(errors), "errors": errors}

    # ------------------------------------------------------------------
    # Field discovery (dynamic — reads the live mapping)
    # ------------------------------------------------------------------
    def get_fields(self) -> list[FieldInfo]:
        resp = self.http.get(f"{self.base_url}/{self.index}/_mapping", timeout=10)
        if not resp.ok:
            raise ElasticsearchError(f"Failed to read mapping for '{self.index}': {resp.text}")
        mapping = resp.json()
        # response is keyed by concrete index name (may differ under aliases)
        first = next(iter(mapping.values()), {})
        props = first.get("mappings", {}).get("properties", {})
        fields: list[FieldInfo] = []
        self._walk_properties(props, "", fields)
        return sorted(fields, key=lambda f: f.name)

    def _walk_properties(self, props: dict, prefix: str, out: list[FieldInfo]) -> None:
        for name, spec in props.items():
            full = f"{prefix}{name}"
            if "properties" in spec:  # object / nested — recurse
                self._walk_properties(spec["properties"], f"{full}.", out)
            else:
                out.append(FieldInfo(name=full, type=spec.get("type", "keyword")))

    # ------------------------------------------------------------------
    # Rule fetch (query pushdown)
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

        body = {"query": {"bool": {"must": must}} if must else {"match_all": {}},
                "size": 10000}
        resp = self.http.post(f"{self.base_url}/{self.index}/_search", json=body, timeout=30)
        if not resp.ok:
            raise ElasticsearchError(f"Search failed on '{self.index}': {resp.text}")
        hits = resp.json().get("hits", {}).get("hits", [])
        return [h["_source"] for h in hits]
