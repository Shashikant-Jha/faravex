"""Browser recording: which project JS/TS functions run in a web page and where the time goes.

No probe in the app: faravex opens Chrome or Edge with a debugging port and its own profile (kept next to the
index, so sign-ins survive between recordings) and records one tab over the DevTools protocol with the V8 sampling
profiler (call timeline, self time) and precise coverage (exact call counts, function ranges). Script positions map to
project files through source maps (Vite, webpack dev builds) or the script's URL path. The trace so far is handed over
every second (the live file) and at stop, in the probe's trace format, so the Record view, latency stats and execution
slice read it like a Python process.

The DevTools connection must stay open while recording (the profiler stops when it closes), so recording lives in the
faravex process that started it: the shared MCP + UI server, or one stdio server.
"""

import base64
import bisect
import json
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config
from .depmap import _Resolver
from .files import iter_source_files, read_text

SAMPLE_US = 200  # sampling interval: calls shorter than this are counted (coverage) but rarely timed
FLUSH = 1.0  # seconds between live updates
MAX_RUNS, MAX_CALLS = 1000, 5000  # as in probe.py
BOUNDARY = {"(idle)", "(program)"}  # the main thread between tasks: a run ends here
ARGS: list[str] = []  # extra browser flags (tests add --headless=new)
log = logging.getLogger("faravex")
_active: dict[Path, dict] = {}
_lock = threading.Lock()


# ---------- Browser and tab ----------
def find_browser() -> str:
    if config.BROWSER:
        return config.BROWSER
    for base in filter(None, (os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"))):
        for sub in (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe",
                    r"Chromium\Application\chrome.exe"):
            if (Path(base) / sub).is_file():
                return str(Path(base) / sub)
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge", "microsoft-edge",
                 "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if found := shutil.which(name):
            return found
    raise ValueError("No Chrome or Edge found; set FARAVEX_BROWSER to the browser's executable")


def _http(port: int, path: str, method: str = "GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _devtools_port(profile: Path) -> int | None:
    """Debugging port of the browser already running on this profile, if it still answers."""
    try:
        port = int((profile / "DevToolsActivePort").read_text().split()[0])
        _http(port, "/json/version")
        return port
    except (OSError, ValueError, IndexError):
        return None


def _browser(profile: Path, url: str) -> int:
    if port := _devtools_port(profile):
        return port
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "DevToolsActivePort").unlink(missing_ok=True)
    # Port 0: the browser picks a free one and writes it to DevToolsActivePort. stdin=DEVNULL: see tracing._run.
    subprocess.Popen([find_browser(), "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-first-run",
                      "--no-default-browser-check", "--disable-background-networking", "--disable-component-update",
                      "--disable-sync", "--no-pings", *ARGS, url],  # the recording window only loads the page you give it
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    while time.time() < deadline:
        if port := _devtools_port(profile):
            return port
        time.sleep(0.2)
    raise ValueError("The browser did not open its debugging port within 20 s")


def _tab(port: int, url: str) -> dict:
    """A tab already on the page's origin (keeps its state), or a new one."""
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(url))
    deadline = time.time() + 3  # a browser just launched with the url is still committing it
    while True:
        for t in _http(port, "/json/list"):
            if t["type"] == "page" and t["url"].startswith(origin):
                return t
        if time.time() > deadline:
            return _http(port, "/json/new?" + urllib.parse.quote(url, safe=":/?=&%#"), "PUT")
        time.sleep(0.2)


def _browser_pid(port: int) -> int:
    try:
        cdp = _CDP(_http(port, "/json/version")["webSocketDebuggerUrl"])
        try:
            info = cdp.call("SystemInfo.getProcessInfo")["processInfo"]
            return next(p["id"] for p in info if p["type"] == "browser")
        finally:
            cdp.close()
    except (OSError, ValueError, KeyError, StopIteration):
        return 0


# ---------- DevTools connection: a minimal WebSocket client (RFC 6455) ----------
class _CDP:
    def __init__(self, ws_url: str) -> None:
        u = urllib.parse.urlsplit(ws_url)
        self.sock = socket.create_connection((u.hostname, u.port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(f"GET {u.path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        self.buf = bytearray()
        while b"\r\n\r\n" not in self.buf:
            self._fill()
        head, _, rest = bytes(self.buf).partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError(f"DevTools refused the connection: {head[:120]!r}")
        self.buf = bytearray(rest)
        self.n = 0
        self.events: list[dict] = []

    def _fill(self) -> None:
        chunk = self.sock.recv(1 << 20)
        if not chunk:
            raise OSError("DevTools closed the connection")
        self.buf += chunk

    def _take(self, n: int) -> bytes:
        while len(self.buf) < n:
            self._fill()
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _send(self, op: int, payload: bytes) -> None:
        n, mask = len(payload), os.urandom(4)
        size = bytes([0x80 | n]) if n < 126 else bytes([0xFE]) + struct.pack(">H", n) if n < 65536 \
            else bytes([0xFF]) + struct.pack(">Q", n)
        self.sock.sendall(bytes([0x80 | op]) + size + mask + bytes(b ^ mask[i & 3] for i, b in enumerate(payload)))

    def _recv(self) -> dict:
        data = bytearray()
        while True:
            b0, b1 = self._take(2)
            n = b1 & 127
            if n == 126:
                n = struct.unpack(">H", self._take(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8))[0]
            payload = self._take(n)  # server frames are never masked
            op = b0 & 15
            if op == 8:
                raise OSError("DevTools closed the connection")
            if op == 9:
                self._send(10, payload)  # ping -> pong
            elif op in (0, 1, 2):
                data += payload
                if b0 & 0x80:
                    return json.loads(data)

    def call(self, method: str, **params) -> dict:
        self.n += 1
        self._send(1, json.dumps({"id": self.n, "method": method, "params": params}).encode())
        while True:
            m = self._recv()
            if m.get("id") == self.n:
                if "error" in m:
                    raise ValueError(f"{method}: {m['error'].get('message')}")
                return m.get("result", {})
            if "method" in m:
                self.events.append(m)

    def close(self) -> None:
        try:
            self._send(8, b"")
        except OSError:
            pass
        self.sock.close()


# ---------- Source maps ----------
B64 = {c: i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/")}


def _vlq(seg: str) -> list[int]:
    out, val, shift = [], 0, 0
    for ch in seg:
        d = B64[ch]
        val += (d & 31) << shift
        if d & 32:
            shift += 5
        else:
            out.append(-(val >> 1) if val & 1 else val >> 1)
            val = shift = 0
    return out


class SourceMap:
    """Generated (line, column) -> (source index, source line), all 0-based. Index maps ("sections") unsupported."""

    def __init__(self, mappings: str) -> None:
        self.lines: list[list[tuple[int, int, int]]] = []
        src = sline = scol = 0
        for text in mappings.split(";"):
            col, segs = 0, []
            for seg in filter(None, text.split(",")):
                v = _vlq(seg)
                col += v[0]
                if len(v) >= 4:
                    src, sline, scol = src + v[1], sline + v[2], scol + v[3]
                    segs.append((col, src, sline))
            self.lines.append(segs)
        self.cols = [[s[0] for s in segs] for segs in self.lines]

    def lookup(self, line: int, col: int) -> tuple[int, int] | None:
        if not 0 <= line < len(self.lines) or not self.lines[line]:
            return None
        _, src, sline = self.lines[line][max(0, bisect.bisect_right(self.cols[line], col) - 1)]
        return src, sline


_loopback: dict[str, str] = {}  # localhost:port -> the loopback address that answered


def _fetch(url: str) -> bytes:
    """GET a URL. For localhost, try 127.0.0.1 and ::1 in turn and remember which one answers: a dev server often
    listens on only one, and on Windows a refused connection takes ~2 s."""
    u = urllib.parse.urlsplit(url)
    hosts = [None] if u.hostname != "localhost" else [h for h in dict.fromkeys([_loopback.get(u.netloc), "127.0.0.1", "[::1]"]) if h]
    err = None
    for host in hosts:
        try:
            target = url if host is None else u._replace(netloc=host + (f":{u.port}" if u.port else "")).geturl()
            with urllib.request.urlopen(target, timeout=5 if host is None else 1) as r:
                if host:
                    _loopback[u.netloc] = host
                return r.read()
        except OSError as e:
            err = e
    raise err


def _load_map(url: str, base: str) -> dict:
    if url.startswith("data:"):
        head, _, body = url.partition(",")
        raw = base64.b64decode(body) if head.endswith(";base64") else urllib.parse.unquote(body).encode()
    else:
        full = urllib.parse.urljoin(base, url)
        if full.startswith("file:"):
            raw = Path(urllib.request.url2pathname(urllib.parse.urlsplit(full).path)).read_bytes()
        elif full.startswith(("http://", "https://")):
            raw = _fetch(full)
        else:
            raise ValueError(f"Can't fetch source map {full}")
    m = json.loads(raw)
    if "mappings" not in m:
        raise ValueError("Indexed source maps are not supported")
    return m


class _Mapper:
    """Generated script position -> (project file, 1-based line), through the script's source map or its URL."""

    def __init__(self, root: Path, cdp: _CDP, scripts: dict[str, dict]) -> None:
        self.root, self.cdp, self.scripts = root, cdp, scripts
        files = [rel for rel, _ in iter_source_files(root)]
        self.res, self.lower = _Resolver(files), {f.lower(): f for f in files}
        self.cache: dict[str, dict] = {}
        self.texts: dict[str, list[str]] = {}

    def source_line(self, rel: str, line: int) -> str:
        if rel not in self.texts:
            self.texts[rel] = (read_text(self.root / rel) or "").split("\n")
        lines = self.texts[rel]
        return lines[line - 1] if 0 < line <= len(lines) else ""

    def file(self, spec: str) -> str | None:
        """Project file for a script URL or source-map source (http://host/src/a.ts, webpack://app/./src/a.ts,
        /@fs/C:/repo/src/a.ts, file:///C:/repo/src/a.ts, C:\\repo\\src\\a.ts, ../src/a.ts)."""
        if re.match(r"^[A-Za-z]:[\\/]", spec):
            path = spec
        else:
            u = urllib.parse.urlsplit(spec)
            path = urllib.parse.unquote(u.path)
        path = re.sub(r"^/([A-Za-z]:/)", r"\1", re.sub(r"^/@fs/", "/", path.replace("\\", "/")))
        if Path(path).is_absolute():
            try:
                rel = os.path.relpath(os.path.normcase(path), os.path.normcase(self.root)).replace("\\", "/")
            except ValueError:  # another drive
                return None
            if not rel.startswith("../"):
                return self.lower.get(rel.lower())
        rel = os.path.normpath("/" + path).replace("\\", "/").lstrip("/")
        return self.res.mention(rel, "", "") if rel else None

    def _script(self, sid: str) -> dict:
        if sid in self.cache:
            return self.cache[sid]
        p = self.scripts.get(sid, {})
        url, smurl = p.get("url", ""), p.get("sourceMapURL", "")
        info = {"map": None, "files": [], "file": None, "starts": None}
        if smurl:
            try:
                m = _load_map(smurl, url)
                base, sroot = (url if smurl.startswith("data:") else urllib.parse.urljoin(url, smurl)), m.get("sourceRoot") or ""
                info["files"] = [self.file(urllib.parse.urljoin(base, (sroot.rstrip("/") + "/" + s) if sroot else s))
                                 for s in m["sources"]]
                info["map"] = SourceMap(m["mappings"])
            except (OSError, ValueError, KeyError, TypeError):
                info["files"] = []
        if not info["map"] and url:
            info["file"] = self.file(url)  # plain script: generated lines are the file's lines
        self.cache[sid] = info
        return info

    def pos(self, sid: str, line0: int, col0: int) -> tuple[str, int] | None:
        s = self._script(sid)
        if s["map"]:
            hit = s["map"].lookup(line0, col0)
            if hit and hit[0] < len(s["files"]) and s["files"][hit[0]]:
                return s["files"][hit[0]], hit[1] + 1
            return None
        return (s["file"], line0 + 1) if s["file"] else None

    def offset(self, sid: str, off: int) -> tuple[int, int]:
        """Character offset -> (line, column), 0-based. shortcut: V8 counts UTF-16 units, so astral characters
        (emoji) earlier in a script shift columns slightly; fine for picking a function's line."""
        s = self._script(sid)
        if s["starts"] is None:
            try:
                text = self.cdp.call("Debugger.getScriptSource", scriptId=sid)["scriptSource"]
            except ValueError:
                text = ""
            s["starts"] = [0] + [m.end() for m in re.finditer("\n", text)]
        i = bisect.bisect_right(s["starts"], off) - 1
        return i, off - s["starts"][i]


# ---------- Profile + coverage -> trace data ----------
RX_CALLEE = re.compile(r"""([\w$]+)\s*\(\s*(?:(['"`])([^'"`]{0,40})\2\s*,\s*)?[^()]*$""")


def anon_name(text: str, line: int) -> str:
    """A name for a function V8 calls anonymous, from its first source line, as DevTools users would describe it:
    `a.addEventListener("click", e => {` -> addEventListener("click") callback; `(function () {` -> IIFE."""
    if m := re.search(r"\bfunction\b", text):
        before = text[:m.start()]
    elif (i := text.find("=>")) >= 0:
        before = re.sub(r"(\([^()]*\)|[\w$]+)\s*$", "", text[:i].rstrip())  # drop the arrow's parameters
    else:
        return f"(anonymous, line {line})"
    before = re.sub(r"\basync\s*$", "", before.rstrip())
    if re.fullmatch(r"\s*[(!+~]*", before):
        return f"IIFE (line {line})"
    if m := RX_CALLEE.search(before):
        return f"{m[1]}({m[2] + m[3] + m[2] if m[2] else ''}) callback (line {line})"
    return f"(anonymous, line {line})"


def convert(profile: dict, coverage: list[dict], mp, wall0: float) -> dict:
    """Trace data (probe.py's format) from a CPU profile and precise coverage. `mp` maps (script id, line, column)
    to (file, line) and offsets to (line, column). A run is one main-thread task that entered project code; a call
    lasts from the first sample with it on the stack to the first without."""
    fns: list[dict] = []
    keys: dict[tuple, int] = {}

    def fn(rel: str, line: int, name: str, top: bool = False) -> int:
        k = (rel, line, name, top)
        if k not in keys:
            keys[k] = len(fns)
            label = name or ("(top level)" if top else anon_name(mp.source_line(rel, line), line))
            fns.append({"file": rel, "name": label, "line": line})
        return keys[k]

    for script in coverage:
        sid = script["scriptId"]
        for f in script["functions"]:
            r = f["ranges"][0]
            if not r["count"] or (not f["functionName"] and r["startOffset"] == 0):
                continue  # did not run, or the script's top level
            a = mp.pos(sid, *mp.offset(sid, r["startOffset"]))
            if not a:
                continue
            i = fn(a[0], a[1], f["functionName"])
            fns[i]["count"] = fns[i].get("count", 0) + r["count"]
            b = mp.pos(sid, *mp.offset(sid, max(r["startOffset"], r["endOffset"] - 1)))
            if b and b[0] == a[0] and b[1] >= a[1]:
                fns[i]["end"] = max(fns[i].get("end", 0), b[1])

    nodes = {n["id"]: n for n in profile["nodes"]}
    parent = {c: n["id"] for n in profile["nodes"] for c in n.get("children", [])}
    paths: dict[int, tuple] = {}

    def project_path(nid: int) -> tuple:
        """((node id, function index), ...) of the project frames on this sample's stack, outermost first."""
        chain = []
        while nid is not None and nid not in paths:
            chain.append(nid)
            nid = parent.get(nid)
        acc = paths.get(nid, ()) if nid is not None else ()
        for x in reversed(chain):
            cf = nodes[x]["callFrame"]
            hit = mp.pos(cf["scriptId"], cf["lineNumber"], cf["columnNumber"]) \
                if cf.get("scriptId") not in (None, "", "0") and cf["lineNumber"] >= 0 else None
            if hit:  # a nameless frame at 0:0 is the script's own top level
                top = not cf["functionName"] and cf["lineNumber"] == 0 and cf["columnNumber"] == 0
                acc = acc + ((x, fn(hit[0], hit[1], cf["functionName"], top)),)
            paths[x] = acc
        return acc

    runs: list[dict] = []
    run, stack = None, []  # stack: [(node id, call index or None past MAX_CALLS)]

    def close_to(depth: int, at: float) -> None:
        while len(stack) > depth:
            _, ci = stack.pop()
            if ci is not None:
                c = run["calls"][ci]
                c[3] = round((at - run["t0"]) / 1000 - c[2], 3)
                run["end"] = at

    def finish(at: float) -> None:
        nonlocal run
        close_to(0, at)
        f = fns[run["calls"][0][0]]
        runs.append({"id": len(runs) + 1, "label": f"{f['file']}:{f['name']}", "start": wall0 + (run["t0"] - t_start) / 1e6,
                     "ms": round((run["end"] - run["t0"]) / 1000, 3), "inputs": "", "output": "", "error": None,
                     "dropped": run["dropped"], "caller": None, "calls": run["calls"]})
        run = None

    t = t_start = profile["startTime"]
    for nid, dt in zip(profile["samples"], profile["timeDeltas"]):
        t += dt
        if nodes[nid]["callFrame"]["functionName"] in BOUNDARY:
            if run:
                finish(t)
            continue
        path = project_path(nid)
        if not path and not run:
            continue
        if run is None:
            run = {"t0": t, "end": t, "calls": [], "dropped": 0}
        k = 0
        while k < min(len(stack), len(path)) and stack[k][0] == path[k][0]:
            k += 1
        close_to(k, t)
        for x, fi in path[k:]:
            up = next((ci for _, ci in reversed(stack) if ci is not None), -1)
            if len(run["calls"]) >= MAX_CALLS:
                run["dropped"] += 1
                stack.append((x, None))
            else:
                run["calls"].append([fi, up, round((t - run["t0"]) / 1000, 3), None, 2 if up < 0 else 0])
                stack.append((x, len(run["calls"]) - 1))
    if run:
        finish(profile["endTime"])
    return {"functions": fns, "runs": runs[-MAX_RUNS:], "lost_runs": max(0, len(runs) - MAX_RUNS), "running": []}


# ---------- Record ----------
def _arm(cdp: _CDP) -> float:
    """Start profiling the tab's renderer; returns the wall time the profile starts at."""
    cdp.call("Profiler.enable")
    cdp.call("Profiler.setSamplingInterval", interval=SAMPLE_US)
    cdp.call("Profiler.startPreciseCoverage", callCount=True, detailed=False)
    cdp.call("Debugger.enable")  # streams scriptParsed (URL, source map) for every script, now and later
    cdp.call("Debugger.setSkipAllPauses", skip=True)  # a `debugger;` statement must not freeze the page
    cdp.call("Profiler.start")
    return time.time()


class _Recorder(threading.Thread):
    """Owns the DevTools connection while recording. Every FLUSH seconds it stops the profiler, restarts it at once
    (a few ms go unsampled), takes the coverage counts since the last flush, and merges them into `data`, which
    on_flush writes as the live trace file: the Record view shows each task within about a second."""

    def __init__(self, root: Path, cdp: _CDP, on_flush) -> None:
        super().__init__(name="browser-recorder", daemon=True)
        self.cdp, self.on_flush = cdp, on_flush
        self.scripts: dict[str, dict] = {}
        self.mapper = _Mapper(root, cdp, self.scripts)
        self.data: dict = {"functions": [], "runs": [], "lost_runs": 0, "running": []}
        self.keys: dict[tuple, int] = {}
        self.halt, self.error = threading.Event(), None
        self.wall = _arm(cdp)

    def _merge(self, chunk: dict) -> None:
        fns, remap = self.data["functions"], []
        for f in chunk["functions"]:
            k = (f["file"], f["line"], f["name"])
            if k not in self.keys:
                self.keys[k] = len(fns)
                fns.append({"file": f["file"], "name": f["name"], "line": f["line"]})
            g = fns[self.keys[k]]
            if "count" in f:
                g["count"] = g.get("count", 0) + f["count"]
            if "end" in f:
                g["end"] = max(g.get("end", 0), f["end"])
            remap.append(self.keys[k])
        runs = self.data["runs"]
        n = runs[-1]["id"] if runs else 0
        for r in chunk["runs"]:  # a task running across a flush is split into two runs
            n += 1
            runs.append({**r, "id": n, "calls": [[remap[c[0]], *c[1:]] for c in r["calls"]]})
        self.data["lost_runs"] += chunk["lost_runs"] + max(0, len(runs) - MAX_RUNS)
        del runs[:-MAX_RUNS]

    def flush(self, last: bool = False) -> None:
        try:
            profile = self.cdp.call("Profiler.stop")["profile"]
        except ValueError:  # the tab moved to another renderer process (another site, a crash): record it there
            self.cdp.events.clear()
            self.wall = _arm(self.cdp)
            return
        wall = self.wall
        if not last:
            self.cdp.call("Profiler.start")
            self.wall = time.time()
        coverage = self.cdp.call("Profiler.takePreciseCoverage")["result"]
        for e in self.cdp.events:
            if e["method"] == "Debugger.scriptParsed":
                p = e["params"]
                if self.scripts.get(p["scriptId"]) != p:  # a new renderer (tab moved site) reuses ids for other scripts
                    self.mapper.cache.pop(p["scriptId"], None)
                self.scripts[p["scriptId"]] = p
        self.cdp.events.clear()
        self._merge(convert(profile, coverage, self.mapper, wall))
        self.on_flush(self.data)

    def run(self) -> None:
        try:
            while not self.halt.wait(FLUSH):
                self.flush()
            self.flush(last=True)
            self.cdp.call("Profiler.stopPreciseCoverage")
            self.cdp.call("Debugger.disable")
        except (OSError, ValueError) as e:  # tab or browser closed: what was flushed so far is kept
            self.error = e
            log.warning("Browser recording ended early: %s", e)


def start(profile_dir: Path, root: Path, url: str, on_flush) -> dict:
    """Record `url` in the browser on `profile_dir`; on_flush(data) gets the trace so far after every flush."""
    with _lock:
        if root in _active:
            raise ValueError("A browser recording is already running for this folder; stop it first")
        _active[root] = None  # reserved while the browser starts
    try:
        port = _browser(profile_dir, url)
        tab = _tab(port, url)
        cdp = _CDP(tab["webSocketDebuggerUrl"])
        rec = _Recorder(root, cdp, on_flush)
        try:
            cdp.call("Page.bringToFront")  # hidden pages pause animation frames and observers: little JS runs
        except ValueError:
            pass
        rec.data.update(pid=_browser_pid(port), argv=["browser", tab["url"]])
    except BaseException:
        with _lock:
            _active.pop(root, None)
        raise
    rec.start()
    with _lock:
        _active[root] = rec
    return {"pid": rec.data["pid"], "tab": tab["url"]}


def stop(root: Path) -> dict:
    with _lock:
        rec = _active.pop(root, None)
    if not rec:
        raise ValueError("The browser recording was lost: faravex restarted while it ran (the recording lives in "
                         "the faravex process). Press Record to start again.")
    rec.halt.set()
    rec.join(timeout=60)
    rec.cdp.close()
    if rec.error and not rec.data["functions"]:
        raise ValueError(f"Lost the recorded browser tab ({rec.error}); was it closed?")
    return rec.data
