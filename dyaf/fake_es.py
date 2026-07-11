"""In-process Elasticsearch test double for development and CI.

Implements the REST subset the platform uses: index creation with
mappings, dynamic mapping updates, _bulk, _count, _refresh, _mapping and
_search (bool / term / terms / range / wildcard / prefix / exists /
match_all + top-level terms aggregations + ES date math). Supports Basic
Auth so credential handling is testable.

Point ELASTICSEARCH_URL at a real cluster and this module is never used.
"""
from __future__ import annotations

import base64
import fnmatch
import json
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


def _as_dt(v) -> Optional[datetime]:
    if not isinstance(v, str):
        return None
    m = re.match(r"^now(?:([+-])(\d+)([smhdw]))?$", v.strip())
    if m:
        now = datetime.now(timezone.utc)
        sign, num, unit = m.groups()
        if not sign:
            return now
        units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
        delta = timedelta(**{units[unit]: int(num)})
        return now - delta if sign == "-" else now + delta
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _cmp(a, b) -> Optional[int]:
    da, db = _as_dt(a), _as_dt(b)
    if da is not None and db is not None:
        return (da > db) - (da < db)
    try:
        fa, fb = float(a), float(b)
        return (fa > fb) - (fa < fb)
    except (TypeError, ValueError):
        pass
    if a is None or b is None:
        return None
    return (str(a) > str(b)) - (str(a) < str(b))


def _eq(stored, value) -> bool:
    if isinstance(stored, bool) or isinstance(value, bool):
        truthy = ("true", "1", "yes")
        return (str(stored).lower() in truthy) == (str(value).lower() in truthy)
    try:
        return float(stored) == float(value)
    except (TypeError, ValueError):
        return str(stored) == str(value)


def _matches(query: dict, doc: dict) -> bool:
    if not query or "match_all" in query:
        return True
    if "bool" in query:
        b = query["bool"]
        if any(not _matches(c, doc) for c in b.get("must", []) + b.get("filter", [])):
            return False
        if any(_matches(c, doc) for c in b.get("must_not", [])):
            return False
        should = b.get("should", [])
        if should:
            needed = b.get("minimum_should_match",
                           0 if (b.get("must") or b.get("filter")) else 1)
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
            if c is None or (op == "gt" and c <= 0) or (op == "gte" and c < 0) \
                    or (op == "lt" and c >= 0) or (op == "lte" and c > 0):
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
        return fnmatch.fnmatchcase(v.lower() if ci else v,
                                   pattern.lower() if ci else pattern)
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
    raise ValueError(f"FakeES: unsupported clause {list(query)}")


def _infer_type(value) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "long"
    if isinstance(value, float):
        return "double"
    if _as_dt(value) is not None:
        return "date"
    return "keyword"


class _State:
    def __init__(self):
        self.indices: dict[str, dict] = {}
        self.auto_id = 0
        self.lock = threading.RLock()


class _Handler(BaseHTTPRequestHandler):
    state: _State
    auth: Optional[str]

    def log_message(self, *args):
        pass

    def _check_auth(self) -> bool:
        if self.auth is None or self.headers.get("Authorization") == self.auth:
            return True
        body = json.dumps({"error": {"type": "security_exception"}}).encode()
        self.send_response(401)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass
        return False

    def _send(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_HEAD(self):
        if self.auth is not None and self.headers.get("Authorization") != self.auth:
            self.send_response(401)
            self.end_headers()
            return
        name = self.path.strip("/").split("/")[0]
        with self.state.lock:
            self.send_response(200 if name in self.state.indices else 404)
        self.end_headers()

    def do_GET(self):
        if not self._check_auth():
            return
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        with self.state.lock:
            if not parts:
                return self._send(200, {"cluster_name": "fake-es",
                                        "version": {"number": "8.14.3"}})
            index = self.state.indices.get(parts[0])
            if index is None:
                return self._send(404, {"error": f"no such index [{parts[0]}]"})
            if len(parts) == 2 and parts[1] == "_mapping":
                return self._send(200, {parts[0]: {"mappings": index["mappings"]}})
            if len(parts) == 2 and parts[1] == "_count":
                return self._send(200, {"count": len(index["docs"])})
        self._send(400, {"error": f"unsupported GET {self.path}"})

    def do_PUT(self):
        if not self._check_auth():
            return
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        body = json.loads(self._body() or b"{}")
        with self.state.lock:
            if len(parts) == 1:
                if parts[0] in self.state.indices:
                    return self._send(400, {"error": {"type": "resource_already_exists_exception"}})
                self.state.indices[parts[0]] = {
                    "mappings": body.get("mappings") or {"properties": {}}, "docs": {}}
                return self._send(200, {"acknowledged": True})
        self._send(400, {"error": f"unsupported PUT {self.path}"})

    def do_DELETE(self):
        if not self._check_auth():
            return
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        with self.state.lock:
            if len(parts) == 1 and parts[0] in self.state.indices:
                del self.state.indices[parts[0]]
                return self._send(200, {"acknowledged": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._check_auth():
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
                    matched = [(i, d) for i, d in index["docs"].items()
                               if _matches(query, d)]
                except ValueError as e:
                    return self._send(400, {"error": str(e)})
                response = {
                    "hits": {"total": {"value": len(matched)},
                             "hits": [{"_index": parts[0], "_id": i, "_source": d}
                                      for i, d in matched[:size]]}}
                aggs = body.get("aggs") or {}
                if aggs:
                    response["aggregations"] = self._aggs(aggs, [d for _, d in matched])
                return self._send(200, response)
        self._send(400, {"error": f"unsupported POST {self.path}"})

    def _aggs(self, aggs: dict, docs: list[dict]) -> dict:
        out = {}
        for name, spec in aggs.items():
            if "terms" not in spec:
                continue
            field = spec["terms"].get("field")
            size = int(spec["terms"].get("size", 10))
            counts: dict = {}
            for d in docs:
                v = d.get(field)
                if v not in (None, ""):
                    counts[v] = counts.get(v, 0) + 1
            buckets = sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[:size]
            out[name] = {"buckets": [{"key": k, "doc_count": c} for k, c in buckets]}
        return out

    def _bulk(self, raw: bytes):
        lines = [ln for ln in raw.decode().splitlines() if ln.strip()]
        items, i = [], 0
        while i < len(lines):
            meta = json.loads(lines[i]).get("index", {})
            index = self.state.indices.get(meta.get("_index"))
            if index is None:
                items.append({"index": {"error": {"type": "index_not_found_exception"}}})
                i += 2
                continue
            doc = json.loads(lines[i + 1])
            _id = str(meta.get("_id") or f"auto-{self.state.auto_id}")
            self.state.auto_id += 1
            index["docs"][_id] = doc
            props = index["mappings"].setdefault("properties", {})
            for k, v in doc.items():
                if k not in props and v is not None:
                    props[k] = {"type": _infer_type(v)}
            items.append({"index": {"_id": _id, "status": 201}})
            i += 2
        self._send(200, {"errors": False, "items": items})


class FakeElasticsearch:
    """Threaded fake ES server; context manager or start()/stop()."""

    def __init__(self, port: int = 0, username: Optional[str] = None,
                 password: Optional[str] = None):
        expected = None
        if username is not None:
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
