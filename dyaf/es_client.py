"""Elasticsearch client (the only detection read path).

Handles connection/auth, first-run index bootstrap, bulk ingest, dynamic
field discovery from the live mapping, value suggestions and query
execution.
"""
from __future__ import annotations

import json
from typing import Optional

import requests


class EsError(RuntimeError):
    pass


class EsClient:
    def __init__(self, base_url: str,
                 username: Optional[str] = None, password: Optional[str] = None,
                 api_key: Optional[str] = None, ca_cert: Optional[str] = None,
                 verify_certs: bool = True):
        self.base_url = base_url.rstrip("/")
        self.http = requests.Session()
        if "localhost" in base_url or "127.0.0.1" in base_url:
            self.http.trust_env = False  # never proxy local clusters
        self.auth_mode = "none"
        if api_key:
            self.http.headers["Authorization"] = f"ApiKey {api_key}"
            self.auth_mode = "api_key"
        elif username is not None:
            self.http.auth = (username, password or "")
            self.auth_mode = "basic"
        if ca_cert:
            self.http.verify = ca_cert
        elif not verify_certs:
            self.http.verify = False
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # ------------------------------------------------------------- cluster
    def check_connection(self) -> dict:
        try:
            resp = self.http.get(self.base_url, timeout=5)
            return {"reachable": True,
                    "authenticated": resp.status_code not in (401, 403),
                    "ok": resp.ok}
        except requests.RequestException as e:
            return {"reachable": False, "authenticated": False, "ok": False,
                    "error": str(e)}

    def ping(self) -> bool:
        return self.check_connection()["ok"]

    # ------------------------------------------------------------- indices
    def index_exists(self, index: str) -> bool:
        return self.http.head(f"{self.base_url}/{index}", timeout=10).status_code == 200

    def create_index(self, index: str, mappings: dict) -> None:
        resp = self.http.put(
            f"{self.base_url}/{index}",
            json={"settings": {"number_of_shards": 1, "number_of_replicas": 0},
                  "mappings": mappings}, timeout=30)
        if not resp.ok:
            raise EsError(f"create index '{index}': {resp.text}")

    def ensure_index(self, index: str, mappings: dict) -> dict:
        if self.index_exists(index):
            return {"index": index, "created": False}
        self.create_index(index, mappings)
        return {"index": index, "created": True}

    def refresh(self, index: str) -> None:
        self.http.post(f"{self.base_url}/{index}/_refresh", timeout=10)

    def count(self, index: str) -> int:
        resp = self.http.get(f"{self.base_url}/{index}/_count", timeout=10)
        return resp.json().get("count", 0) if resp.ok else 0

    def get_mapping_fields(self, index: str) -> list[dict]:
        """Dynamic field discovery: [{name, type}] from the live mapping."""
        resp = self.http.get(f"{self.base_url}/{index}/_mapping", timeout=10)
        if not resp.ok:
            raise EsError(f"mapping for '{index}': {resp.text}")
        first = next(iter(resp.json().values()), {})
        fields: list[dict] = []

        def walk(props: dict, prefix: str):
            for name, spec in props.items():
                full = f"{prefix}{name}"
                if "properties" in spec:
                    walk(spec["properties"], f"{full}.")
                else:
                    fields.append({"name": full, "type": spec.get("type", "keyword")})

        walk(first.get("mappings", {}).get("properties", {}), "")
        return sorted(fields, key=lambda f: f["name"])

    # ------------------------------------------------------------- ingest
    def bulk_index(self, index: str, docs: list[dict],
                   id_field: Optional[str] = None, refresh: bool = True) -> dict:
        if not docs:
            return {"indexed": 0, "errors": []}
        lines = []
        for doc in docs:
            action: dict = {"index": {"_index": index}}
            if id_field and doc.get(id_field) is not None:
                action["index"]["_id"] = str(doc[id_field])
            lines.append(json.dumps(action))
            lines.append(json.dumps(doc, default=str))
        resp = self.http.post(f"{self.base_url}/_bulk",
                              data=("\n".join(lines) + "\n").encode(),
                              headers={"Content-Type": "application/x-ndjson"},
                              timeout=60)
        if not resp.ok:
            raise EsError(f"bulk: {resp.text}")
        errors = [item["index"]["error"] for item in resp.json().get("items", [])
                  if item.get("index", {}).get("error")]
        if refresh:
            self.refresh(index)
        return {"indexed": len(docs) - len(errors), "errors": errors}

    # ------------------------------------------------------------- queries
    def search(self, index: str, body: dict) -> dict:
        resp = self.http.post(f"{self.base_url}/{index}/_search", json=body, timeout=30)
        if not resp.ok:
            raise EsError(f"search on '{index}': {resp.text}")
        return resp.json()

    def fetch_docs(self, index: str, query: dict, size: int = 10000) -> list[dict]:
        result = self.search(index, {"query": query, "size": size})
        return [h["_source"] for h in result.get("hits", {}).get("hits", [])]

    def field_values(self, index: str, field: str, size: int = 50) -> list:
        body = {"size": 0, "aggs": {"v": {"terms": {"field": field, "size": size}}}}
        result = self.search(index, body)
        buckets = result.get("aggregations", {}).get("v", {}).get("buckets", [])
        return [b.get("key_as_string", b.get("key")) for b in buckets]
