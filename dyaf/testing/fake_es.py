"""In-process Elasticsearch test double.

Implements the exact REST subset the platform uses — index creation with
mappings, mapping introspection, dynamic mapping updates, _bulk ingest,
_count, _refresh and _search with the query DSL produced by
dyaf.rules.query_builder (bool / term / terms / range / wildcard / prefix /
exists / match_all).

Purpose: local development and CI environments without a real cluster.
The application code is identical either way — point ELASTICSEARCH_URL at
a real cluster and this module is never imported.
"""
from __future__ import annotations

import fnmatch
import json
import re
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


# ----------------------------------------------------------------------
# Value comparison helpers (mirror ES semantics loosely)
# ----------------------------------------------------------------------

def _as_datetime(v) -> Optional[datetime]:
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _cmp(a, b) -> Optional[int]:
    """Compare with date > numeric > string precedence; None = incomparable."""
    da, db_ = _as_datetime(a), _as_datetime(b)
    if da is not None and db_ is not None:
        return (da > db_) - (da < db_)
    try:
        fa, fb = float(a), float(b)
        return (fa > fb) - (fa < fb)
    except (TypeError, ValueError):
        pass
    if a is None or b is None:
        return None
    sa, sb = str(a), str(b)
    return (sa > sb) - (sa < sb)


def _eq(stored, value) -> bool:
    if isinstance(stored, bool) or isinstance(value, bool):
        truthy = ("true", "1", "yes", True, 1)
        return (stored in truthy or str(stored).lower() in truthy) == \
               (value in truthy or str(value).lower() in truthy)
    try:
        return float(stored) == float(value)
    except (TypeError, ValueError):
        return str(stored) == str(value)


# ----------------------------------------------------------------------
# Query evaluation
# ----------------------------------------------------------------------

def _matches(query: dict, doc: dict) -> bool:
    if not query or "match_all" in query:
        return True
    if "bool" in query:
        b = query["bool"]
        for clause in b.get("must", []) + b.get("filter", []):
            if not _matches(clause, doc):
                return False
        for clause in b.get("must_not", []):
            if _matches(clause, doc):
                return False
        should = b.get("should", [])
        if should:
            needed = b.get("minimum_should_match", 0 if (b.get("must") or b.get("filter")) else 1)
            if sum(1 for c in should if _matches(c, doc)) < int(needed):
                return False
        return True
    if "term" in query:
        (field, spec), = query["term"].items()
        value = spec.get("value") if isinstance(spec, dict) else spec
        return doc.get(field) is not None and _eq(doc[field], value)
    if "terms" in query:
        (field, values), = query["terms"].items()
        return doc.get(field) is not None and any(_eq(doc[field], v) for v in values)
    if "range" in query:
        (field, spec), = query["range"].items()
        v = doc.get(field)
        if v is None:
            return False
        for op, bound in spec.items():
            if op not in ("gt", "gte", "lt", "lte"):
                continue
            c = _cmp(v, bound)
            if c is None:
                return False
            if (op == "gt" and c <= 0) or (op == "gte" and c < 0) or \
               (op == "lt" and c >= 0) or (op == "lte" and c > 0):
                return False
        return True
    if "wildcard" in query:
        (field, spec), = query["wildcard"].items()
        pattern = spec.get("value") if isinstance(spec, dict) else spec
        ci = isinstance(spec, dict) and spec.get("case_insensitive")
        v = doc.get(field)
        if v is None:
            return False
        v, pattern = str(v), str(pattern)
        return fnmatch.fnmatchcase(v.lower() if ci else v, pattern.lower() if ci else pattern)
    if "prefix" in query:
        (field, spec), = query["prefix"].items()
        pattern = spec.get("value") if isinstance(spec, dict) else spec
        ci = isinstance(spec, dict) and spec.get("case_insensitive")
        v = doc.get(field)
        if v is None:
            return False
        v, pattern = str(v), str(pattern)
        return (v.lower() if ci else v).startswith(pattern.lower() if ci else pattern)
    if "exists" in query:
        v = doc.get(query["exists"]["field"])
        return v is not None and v != ""
    raise ValueError(f"FakeES: unsupported query clause {list(query)}")


def _infer_type(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "long"
    if isinstance(value, float):
        return "double"
    if _as_datetime(value) is not None:
        return "date"
    return "keyword"


# ----------------------------------------------------------------------
# Server
# ----------------------------------------------------------------------

class _State:
    def __init__(self):
        self.indices: dict[str, dict] = {}   # name -> {"mappings": ..., "docs": {id: doc}}
        self.auto_id = 0
        self.lock = threading.RLock()


class _Handler(BaseHTTPRequestHandler):
    state: _State          # set by factory
    auth: Optional[str]    # expected "Basic <b64>" header value, or None

    def log_message(self, *args):  # silence
        pass

    # -------------------------------------------------- helpers
    def _authorized(self) -> bool:
        if self.auth is None:
            return True
        if self.headers.get("Authorization") == self.auth:
            return True
        self._send(401, {"error": {"type": "security_exception",
                                   "reason": "missing or invalid credentials"}})
        return False

    def _send(self, code: int, payload: dict | list) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _register_fields(self, index: dict, doc: dict) -> None:
        props = index["mappings"].setdefault("properties", {})
        for k, v in doc.items():
            if k not in props and v is not None:
                props[k] = {"type": _infer_type(v)}

    # -------------------------------------------------- verbs
    def do_HEAD(self):
        if self.auth is not None and self.headers.get("Authorization") != self.auth:
            self.send_response(401)
            self.end_headers()
            return
        name = self.path.strip("/").split("/")[0]
        with self.state.lock:
            exists = name in self.state.indices
        self.send_response(200 if exists else 404)
        self.end_headers()

    def do_GET(self):
        if not self._authorized():
            return
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        with self.state.lock:
            if not parts:
                return self._send(200, {"name": "fake-es", "cluster_name": "fake-es",
                                        "version": {"number": "8.14.3"},
                                        "tagline": "You Know, for Search (test double)"})
            index = self.state.indices.get(parts[0])
            if index is None:
                return self._send(404, {"error": f"no such index [{parts[0]}]"})
            if len(parts) == 2 and parts[1] == "_mapping":
                return self._send(200, {parts[0]: {"mappings": index["mappings"]}})
            if len(parts) == 2 and parts[1] == "_count":
                return self._send(200, {"count": len(index["docs"])})
        self._send(400, {"error": f"unsupported GET {self.path}"})

    def do_PUT(self):
        if not self._authorized():
            return
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        body = json.loads(self._body() or b"{}")
        with self.state.lock:
            if len(parts) == 1:
                name = parts[0]
                if name in self.state.indices:
                    return self._send(400, {"error": {"type": "resource_already_exists_exception"}})
                self.state.indices[name] = {"mappings": body.get("mappings") or {"properties": {}},
                                            "docs": {}}
                return self._send(200, {"acknowledged": True, "index": name})
        self._send(400, {"error": f"unsupported PUT {self.path}"})

    def do_DELETE(self):
        if not self._authorized():
            return
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        with self.state.lock:
            if len(parts) == 1 and parts[0] in self.state.indices:
                del self.state.indices[parts[0]]
                return self._send(200, {"acknowledged": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            return
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        raw = self._body()
        with self.state.lock:
            if parts == ["_bulk"]:
                return self._bulk(raw)
            if len(parts) == 2 and parts[1] == "_refresh":
                return self._send(200, {"_shards": {"successful": 1}})
            if len(parts) == 2 and parts[1] == "_search":
                index = self.state.indices.get(parts[0])
                if index is None:
                    return self._send(404, {"error": f"no such index [{parts[0]}]"})
                body = json.loads(raw or b"{}")
                query = body.get("query", {"match_all": {}})
                size = int(body.get("size", 10))
                try:
                    hits = [{"_index": parts[0], "_id": _id, "_source": doc}
                            for _id, doc in index["docs"].items() if _matches(query, doc)]
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(200, {"hits": {"total": {"value": len(hits)},
                                                 "hits": hits[:size]}})
        self._send(400, {"error": f"unsupported POST {self.path}"})

    def _bulk(self, raw: bytes):
        lines = [ln for ln in raw.decode("utf-8").splitlines() if ln.strip()]
        items, i = [], 0
        while i < len(lines):
            action = json.loads(lines[i])
            if "index" not in action:
                return self._send(400, {"error": "only index actions supported"})
            meta = action["index"]
            index = self.state.indices.get(meta.get("_index"))
            if index is None:
                items.append({"index": {"_id": meta.get("_id"),
                                        "error": {"type": "index_not_found_exception"}}})
                i += 2
                continue
            doc = json.loads(lines[i + 1])
            _id = str(meta.get("_id") or f"auto-{self.state.auto_id}")
            self.state.auto_id += 1
            index["docs"][_id] = doc
            self._register_fields(index, doc)
            items.append({"index": {"_id": _id, "result": "created", "status": 201}})
            i += 2
        self._send(200, {"errors": any("error" in it["index"] for it in items), "items": items})


class FakeElasticsearch:
    """Threaded fake ES server; use as a context manager or start()/stop()."""

    def __init__(self, port: int = 0, username: Optional[str] = None,
                 password: Optional[str] = None):
        expected = None
        if username is not None:
            import base64
            token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
            expected = f"Basic {token}"
        handler = type("Handler", (_Handler,), {"state": _State(), "auth": expected})
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "FakeElasticsearch":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
