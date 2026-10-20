"""Browser UI: a local HTTP server (stdlib only) next to the MCP stdio server.

    python server.py ui [--port 8765] [--no-browser]

Binds 127.0.0.1 only and rejects requests whose Host is not loopback (blocks DNS rebinding),
so other machines and web pages cannot read your folders through it.
"""

import json
import logging
import mimetypes
import os
import string
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, depmap, navigate, tracing
from .files import read_text

log = logging.getLogger("faravex")
WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_PREVIEW_LINES = 400
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")


def _roots() -> list[str]:
    if os.name == "nt":
        return [f"{d}:\\" for d in string.ascii_uppercase if os.path.isdir(f"{d}:\\")]
    return ["/"]


def browse(path: str | None) -> dict:
    """Sub-folders of `path` (or drives / home when empty) for the folder picker."""
    if not path:
        return {"path": "", "parent": None, "dirs": [str(Path.home())] + _roots()}
    p = Path(path).expanduser().resolve()
    while not p.is_dir() and p.parent != p:  # a typo or a deleted folder: open the nearest one that exists
        p = p.parent
    if not p.is_dir():
        return browse(None)
    dirs = []
    try:
        with os.scandir(p) as it:
            for e in it:
                try:
                    if e.is_dir() and not e.name.startswith((".", "$")):
                        dirs.append(e.name)
                except OSError:
                    pass
    except PermissionError:
        pass
    parent = str(p.parent) if p.parent != p else ""
    return {"path": str(p), "parent": parent, "dirs": sorted(dirs, key=str.lower)}


def read_file(root: str, rel: str) -> dict:
    base = Path(root).resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise ValueError("File is outside the project")
    text = read_text(target)
    if text is None:
        raise ValueError("Binary or too large")
    lines = text.splitlines()
    return {"path": rel, "lines": len(lines), "text": "\n".join(lines[:MAX_PREVIEW_LINES]), "truncated": len(lines) > MAX_PREVIEW_LINES}


def graph(path: str) -> dict:
    if not path or not Path(path).is_dir():
        raise ValueError("Pick a folder first")
    return depmap.build(Path(path))


def context(root: str, rel: str) -> dict:
    base = Path(root).resolve()
    if not base.is_dir() or not (base / rel).resolve().is_relative_to(base):
        raise ValueError("File is outside the project")
    text = navigate.context_slice(base, rel)
    return {"text": text, "chars": len(text)}


def _built_index(root: str):
    from . import server

    if not root or not Path(root).is_dir():
        raise ValueError("Pick a folder first")
    index = server._get_index(root)
    if not index.is_built:
        raise ValueError("This folder is not indexed yet: press Index first")
    return index


def search(root: str, query: str, k: str = "15") -> dict:
    """Semantic search for the map: re-embeds changed files first (like the MCP tool), never builds from scratch."""
    if not query.strip():
        raise ValueError("Type what you are looking for")
    index = _built_index(root)
    index.update()
    hits = index.search(query, top_k=max(1, min(int(k), 50)))
    return {"hits": [{"path": h.path, "start": h.start_line, "end": h.end_line, "score": round(h.score, 3), "match": h.match, "scope": h.scope,
                      "preview": "\n".join(h.text.splitlines()[:3])} for h in hits]}


def diff(root: str, base: str = "") -> dict:
    from . import changes

    index = _built_index(root)
    return changes.analyse(index.root, index, base=base or None)


def info() -> dict:
    """Paths for the MCP setup snippets in the guide."""
    return {"python": sys.executable, "server": str(config.APP_DIR / "server.py")}


def trace_get(root: str, action: str = "show", session: str = "", run: str = "", against: str = "") -> dict:
    """Live trace for the UI: status, a trace with its runs (so far, while recording), an execution slice, or a
    comparison of `session` with `against`."""
    if not root:
        raise ValueError("Pick a folder first")
    base = tracing.project_root(root)
    if action == "compare":
        return tracing.compare(base, against, session)
    if action == "status":
        return tracing.status(base)
    t = tracing.load(base, session or None)
    if action == "slice":
        text = tracing.execution_slice(base, t, run_id=run or None)
        return {"text": text, "chars": len(text)}
    return {**t, "entries": tracing.entry_stats(t)}


def trace_post(root: str, action: str, port=None, url=None, session=None) -> dict:
    if not root:
        raise ValueError("Pick a folder first")
    base = tracing.project_root(root)
    if action in ("pin", "unpin"):
        return {"message": tracing.pin(base, str(session or ""), action == "pin")}
    if action == "stop":
        t = tracing.stop(base)
        return {"message": tracing.summary_text(base, t).split("\n")[0], "trace": {**t, "entries": tracing.entry_stats(t)}}
    if action not in ("start", "enable", "disable"):
        raise ValueError("Unknown trace action")
    return {"message": tracing.run(action, base, int(port) if port else None, url=url or None)}


def calls(limit: str = "300") -> dict:
    """MCP tool calls from every server process, newest first, with totals per tool (all calls, not just these)."""
    try:
        lines = config.CALLS_LOG.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    rows, tools = [], {}
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:  # a line cut short by a crash
            continue
        rows.append(r)
        t = tools.setdefault(r["tool"], {"tool": r["tool"], "calls": 0, "in": 0, "out": 0, "ms": 0.0})
        t["calls"] += 1
        for k in ("in", "out", "ms"):
            t[k] += r[k]
    return {"calls": rows[::-1][:int(limit)], "count": len(rows),
            "tools": sorted(tools.values(), key=lambda t: -t["out"]), "log": str(config.CALLS_LOG)}


def run_index(root: str) -> dict:
    from . import server

    index = server._get_index(root)
    return {**index.update(), **index.status()}


class Handler(BaseHTTPRequestHandler):
    server_version = "faravex"

    def log_message(self, fmt, *args):  # stdout stays quiet; requests go to the debug log
        log.debug("%s %s", self.address_string(), fmt % args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].lower()
        return host in LOOPBACK

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _api(self, fn, *args) -> None:
        try:
            self._json(fn(*args))
        except (ValueError, OSError) as e:
            self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
        except Exception as e:  # keep the server alive; report the failure to the page
            log.exception("API error")
            self._json({"error": f"{type(e).__name__}: {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/api/browse":
            return self._api(browse, q.get("path"))
        if url.path == "/api/graph":
            return self._api(graph, q.get("path", ""))
        if url.path == "/api/file":
            return self._api(read_file, q.get("root", ""), q.get("path", ""))
        if url.path == "/api/context":
            return self._api(context, q.get("root", ""), q.get("path", ""))
        if url.path == "/api/info":
            return self._api(info)
        if url.path == "/api/diff":
            return self._api(diff, q.get("root", ""), q.get("base", ""))
        if url.path == "/api/search":
            return self._api(search, q.get("root", ""), q.get("q", ""), q.get("k", "15"))
        if url.path == "/api/trace":
            return self._api(trace_get, q.get("root", ""), q.get("action", "show"), q.get("session", ""), q.get("run", ""),
                             q.get("base", ""))
        if url.path == "/api/calls":
            return self._api(calls, q.get("limit", "300"))
        self._static(url.path)

    def do_POST(self) -> None:
        if not self._host_ok() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._json({"error": "Body must be a JSON object"}, HTTPStatus.BAD_REQUEST)
        if urlparse(self.path).path == "/api/index" and Path(body.get("path", "")).is_dir():
            return self._api(run_index, body["path"])
        if urlparse(self.path).path == "/api/trace":
            return self._api(trace_post, body.get("path", ""), body.get("action", ""), body.get("port"), body.get("url"),
                             body.get("session"))
        self._json({"error": "Unknown request"}, HTTPStatus.NOT_FOUND)

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if not target.is_relative_to(WEB_DIR) or not target.is_file():
            return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith(("javascript", "json")):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype)


def serve(port: int | None = None, open_browser: bool = True) -> None:
    port = port or config.HTTP_PORT
    mimetypes.add_type("text/javascript", ".js")  # Windows registry can map .js to text/plain
    mimetypes.add_type("font/woff2", ".woff2")
    mimetypes.add_type("image/svg+xml", ".svg")
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    log.info("faravex UI on %s (Ctrl+C to stop)", url)
    print(f"faravex UI: {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
