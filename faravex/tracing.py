"""Live execution tracing: which project functions actually run when you use the app, and where the time goes.

Python 3.12+ apps: enable() puts faravex_trace.pth into the app's venv, so probe.py loads (idle) whenever the app
starts; start(port) records the process listening on that port. Web pages: start(url=...) records the JS/TS a tab
runs (browser.py). stop() returns what ran. Trace files live next to the index:
<index dir>/trace/<session>.<pid>.json (".live.json" while recording), the last KEEP sessions kept.
"""

import ast
import json
import math
import os
import posixpath
import re
import statistics
import subprocess
import time
from pathlib import Path

from . import browser, config, depmap, navigate
from .files import def_start, read_text
from .index import index_dir_for

PTH = "faravex_trace.pth"
KEEP = 50
HEARTBEAT = 6  # seconds; probes touch their registry file every 2 s
RX_FILE = re.compile(r"^(\d{8}-\d{6})\.(\d+)(\.live)?\.json$")


def project_root(path: str | None) -> Path:
    root = Path(path).expanduser() if path else config.DEFAULT_ROOT
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    return root.resolve()


def trace_dir(root: Path) -> Path:
    return index_dir_for(root) / "trace"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    # stdin=DEVNULL: inside the MCP server stdin is the protocol pipe; a child inheriting it can hang.
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                          timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


# ---------- Enable / disable ----------
def _venv(root: Path) -> Path:
    """The app's venv: in the folder, the nearest parent (a map of a subfolder uses the repo's venv), or a direct
    subfolder (a repo whose app lives in backend/ or similar)."""
    names = (".venv", "venv", "env")
    for d in [root, *root.parents]:
        for name in names:
            if (d / name / "pyvenv.cfg").is_file():
                return d / name
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        for name in names:
            if (d / name / "pyvenv.cfg").is_file():
                return d / name
    raise ValueError(f"No virtual environment in {root}, its parents or its subfolders "
                     "(looked for .venv, venv and env with a pyvenv.cfg)")


def _enabled(root: Path) -> bool:
    """Enabled for this folder: a venv has one .pth, which may point at another folder that uses the same venv."""
    try:
        pth = _pth(root)
        return pth.is_file() and repr(str(trace_dir(root))) in pth.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False


def _pth(root: Path) -> Path:
    venv = _venv(root)
    site = venv / "Lib" / "site-packages"
    if not site.is_dir():
        site = next(iter(sorted(venv.glob("lib/python3.*/site-packages"), reverse=True)), None)
        if site is None:
            raise ValueError(f"No site-packages folder in {venv}")
    return site / PTH


def enable(root: Path) -> str:
    venv = _venv(root)
    m = re.search(r"^\s*version(?:_info)?\s*=\s*(\d+)\.(\d+)", (venv / "pyvenv.cfg").read_text(errors="ignore"), re.M)
    if not m or (int(m[1]), int(m[2])) < (3, 12):
        raise ValueError(f"Tracing needs Python 3.12 or newer (sys.monitoring); {venv} runs "
                         f"{m[1] + '.' + m[2] if m else 'an unknown version'}")
    pth, tdir = _pth(root), trace_dir(root)
    tdir.mkdir(parents=True, exist_ok=True)
    moved = " (it traced another folder before; one folder per venv)" if pth.is_file() and not _enabled(root) else ""
    probe = Path(__file__).with_name("probe.py")
    # One `import` line: site.py executes it at every interpreter start in this venv.
    pth.write_text(f"import os, runpy; p = {str(probe)!r}; os.path.isfile(p) and runpy.run_path(p, "
                   f"{{'ROOT': {str(root)!r}, 'DIR': {str(tdir)!r}}})\n", encoding="utf-8")
    return (f"Tracing enabled for {root.name}: {pth}{moved}{_gitignore(venv.parent, pth)}. "
            "Restart the app once, then start a trace with its port.")


def _gitignore(root: Path, pth: Path) -> str:
    try:
        rel = pth.relative_to(root).as_posix()
    except ValueError:
        return ""  # venv outside the project: nothing in the repo to ignore
    try:
        if _run(["git", "-C", str(root), "check-ignore", "-q", rel]).returncode == 0:
            return " (already ignored by git)"
    except (OSError, subprocess.SubprocessError):
        pass  # no git: decide from .gitignore alone
    gi = root / ".gitignore"
    data = gi.read_bytes() if gi.is_file() else b""
    if rel in data.decode(errors="ignore").splitlines():
        return ""
    nl = b"\r\n" if b"\r\n" in data else b"\n"
    with open(gi, "ab") as fh:
        fh.write((nl if data and not data.endswith(b"\n") else b"") + b"# faravex trace probe" + nl + rel.encode() + nl)
    return f" (added {rel} to .gitignore)"


def disable(root: Path) -> str:
    pth = _pth(root)
    if not pth.is_file():
        return "Tracing was not enabled."
    pth.unlink()
    return f"Tracing disabled: removed {pth}. Apps already running keep an idle probe until restarted."


# ---------- Processes ----------
def port_pid(port: int) -> int:
    """Process listening on a local TCP port."""
    if os.name == "nt":
        for line in _run(["netstat", "-ano"]).stdout.splitlines():
            p = line.split()
            # listening rows have a :0 foreign port; the state column is localized, so it is not used
            if len(p) == 5 and p[0] == "TCP" and p[1].rsplit(":", 1)[-1] == str(port) and p[2].endswith(":0"):
                return int(p[4])
    else:
        out = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"]).stdout.split()
        if out:
            return int(out[0])
    raise ValueError(f"Nothing is listening on port {port}")


def _probes(tdir: Path) -> dict[int, dict]:
    out = {}
    for f in (tdir / "probes").glob("*.json"):
        try:
            if time.time() - f.stat().st_mtime < HEARTBEAT:
                d = json.loads(f.read_text(encoding="utf-8"))
                out[d["pid"]] = d
        except (OSError, ValueError, KeyError):
            pass
    return out


def status(root: Path) -> dict:
    tdir = trace_dir(root)
    try:
        enabled, pth, venv = _enabled(root), str(_pth(root)), True
    except ValueError as e:
        enabled, pth, venv = False, str(e), False
    try:
        recording = json.loads((tdir / "control.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        recording = None
    ss = _sessions(tdir)
    infos = {x: app_info(tdir, x) for x in ss}
    return {"enabled": enabled, "pth": pth, "venv": venv, "probes": list(_probes(tdir).values()), "recording": recording,
            "sessions": ss, "apps": {x: app_text(i) for x, i in infos.items()},
            "pinned": [x for x, i in infos.items() if i.get("pinned")]}


def status_text(root: Path) -> str:
    s = status(root)
    out = [f"tracing: {'enabled' if s['enabled'] else 'not enabled'} ({s['pth']})",
           f"processes with the probe: {', '.join(str(p['pid']) + ' ' + ' '.join(p['argv'])[:80] for p in s['probes']) or '-'}",
           f"recording: {s['recording']['session'] + ' (port ' + str(s['recording']['port']) + ')' if s['recording'] else 'no'}",
           "recent traces: " + (", ".join(f"{x} ({s['apps'][x]}{', pinned' if x in s['pinned'] else ''})"
                                          for x in s["sessions"][-20:]) or "-")]
    return "\n".join(out)


def pin(root: Path, session: str, on: bool = True) -> str:
    """Pinned recordings are never cleaned up (a baseline to compare against). Stored in the recording's own
    local <session>.app.json; nothing leaves the machine."""
    tdir = trace_dir(root)
    if session not in _sessions(tdir):
        raise ValueError(f"No recording {session}")
    _write(tdir / f"{session}.app.json", {**app_info(tdir, session), "pinned": on})
    return f"{'Pinned' if on else 'Unpinned'} recording {session}."


def _cleanup(tdir: Path) -> None:
    """Keep the newest KEEP recordings plus every pinned one."""
    loose = [s for s in _sessions(tdir) if not app_info(tdir, s).get("pinned")]
    for old in loose[:-KEEP]:
        for f in tdir.glob(f"{old}.*"):
            f.unlink(missing_ok=True)


# ---------- Record ----------
def start(root: Path, port: int | None = None, url: str | None = None) -> str:
    tdir = trace_dir(root)
    if (tdir / "control.json").is_file():
        raise ValueError("A trace is already recording; stop it first")
    why = ""
    if port and not url:
        try:
            _venv(root)
        except ValueError:  # no Python app here: the port serves a web page (static site, dev server); record that
            port_pid(port)  # nothing listening: say so before opening a browser
            url, why = f"http://localhost:{port}/", f"No Python venv in {root.name}, so port {port} is recorded as a web page. "
    if url:
        if not url.startswith(("http://", "https://", "file:")):
            raise ValueError("url must start with http://, https:// or file:")
        tdir.mkdir(parents=True, exist_ok=True)
        session = time.strftime("%Y%m%d-%H%M%S")
        b = browser.start(tdir / "browser-profile", root, url,
                          lambda d: _write(tdir / f"{session}.{d['pid']}.live.json", {**d, "session": session}))
        _write(tdir / f"{session}.app.json", {"app": root.name, "port": None, "cmd": f"browser {b['tab']}"[:120]})
        _write(tdir / "control.json", {"session": session, "pids": [b["pid"]], "port": None, "browser": True,
                                       "started": time.time()})
        return (f"{why}Recording browser trace {session}: {b['tab']} in the Chrome window faravex opened (its own profile, "
                f"browser process {b['pid']}; your other browser windows are not recorded). Use the page there: each task "
                "shows up within a second. Keep that window visible (hidden pages pause animations and observers).")
    owner, probes = port_pid(port), _probes(tdir)
    pids = {owner}
    while True:  # plus children of the listener (reloaders and venv launchers run the app in a child process)
        more = {p for p, d in probes.items() if d.get("ppid") in pids} - pids
        if not more:
            break
        pids |= more
    traced = sorted(p for p in pids if p in probes)
    if not traced:
        hint = ("Restart the app: it started before tracing was enabled, or runs from another venv" if _enabled(root)
                else "Enable tracing for this folder, then restart the app")
        raise ValueError(f"Process {owner} on port {port} has no faravex probe. {hint}.")
    session = time.strftime("%Y%m%d-%H%M%S")
    _write(tdir / f"{session}.app.json", {"app": root.name, "port": port, "cmd": _cmd(probes[traced[-1]]["argv"])})
    _write(tdir / "control.json", {"session": session, "pids": traced, "port": port, "started": time.time()})
    deadline = time.time() + 3  # each probe writes its live file the moment it starts recording
    while time.time() < deadline and not all((tdir / f"{session}.{p}.live.json").is_file() for p in traced):
        time.sleep(0.05)
    late = [p for p in traced if not (tdir / f"{session}.{p}.live.json").is_file()]
    note = f" (process {', '.join(map(str, late))} has not confirmed yet)" if late else ""
    return f"Recording trace {session}: port {port}, process {', '.join(map(str, traced))}{note}. Use the app, then stop."


def stop(root: Path) -> dict:
    tdir = trace_dir(root)
    try:
        c = json.loads((tdir / "control.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("No trace is recording") from None
    (tdir / "control.json").unlink()
    if c.get("browser"):
        data = browser.stop(root)
        _write(tdir / f"{c['session']}.{data['pid']}.json", {**data, "session": c["session"]})
        (tdir / f"{c['session']}.{data['pid']}.live.json").unlink(missing_ok=True)
    deadline = time.time() + 3  # probes look for the control file 4x a second
    while time.time() < deadline and not all((tdir / f"{c['session']}.{p}.json").is_file() for p in c["pids"]):
        time.sleep(0.1)
    _cleanup(tdir)
    return load(root, c["session"])


def _cmd(argv: list[str]) -> str:
    """`uvicorn app.main:app --port 8000` from argv (`python -m x` reports x's __main__.py path)."""
    first = Path(argv[0]) if argv else Path("python")
    name = first.parent.name if first.stem == "__main__" else first.stem
    return " ".join([name, *argv[1:]])[:120]


def app_info(tdir: Path, session: str) -> dict:
    """Which app a recording came from: folder name, port, command. Recordings made before this was kept
    get it from their process argv (no port)."""
    try:
        return json.loads((tdir / f"{session}.app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    for f in sorted(tdir.glob(f"{session}.*.json")):
        if RX_FILE.match(f.name):
            try:
                argv = json.loads(f.read_text(encoding="utf-8")).get("argv", [])
            except (OSError, ValueError):
                continue
            info = {"app": tdir.parent.name.rsplit("-", 1)[0], "port": None, "cmd": _cmd(argv)}
            _write(tdir / f"{session}.app.json", info)  # once: data files can be large
            return info
    return {"app": "", "port": None, "cmd": ""}


def app_text(a: dict) -> str:
    return " · ".join(x for x in (a["app"], f":{a['port']}" if a.get("port") else "", a["cmd"]) if x)


def _sessions(tdir: Path) -> list[str]:
    return sorted({m[1] for f in tdir.glob("*.json") if (m := RX_FILE.match(f.name))})


def load(root: Path, session: str | None = None) -> dict:
    """One trace, all its processes merged. functions: in order of first call, with calls, exits by exception,
    total and self ms. edges: [caller, callee, calls] (caller -1 = an entry). runs: one per entry into the project
    (a request), each with calls [function, parent call, start ms, ms or None while running, flags, self ms]
    where flags 1 = raised, 2 = entry call."""
    tdir = trace_dir(root)
    try:
        live = json.loads((tdir / "control.json").read_text(encoding="utf-8"))["session"]
    except (OSError, ValueError, KeyError):
        live = None
    session = session or live or next(iter(_sessions(tdir)[::-1]), None)
    if not session:
        raise ValueError("No traces yet: start one with the app's port")
    if not re.fullmatch(r"\d{8}-\d{6}", session):  # it becomes a glob: no "../" or "*" from a client
        raise ValueError(f"No recording {session}")
    parts: dict[str, dict] = {}  # pid -> data; the final file wins over the live one
    for f in sorted(tdir.glob(f"{session}.*.json"), key=lambda f: f.name.endswith(".live.json")):
        if (m := RX_FILE.match(f.name)) and m[2] not in parts:
            try:
                parts[m[2]] = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    keys: dict[tuple, int] = {}
    fns: list[dict] = []
    runs: list[dict] = []
    for pid, data in parts.items():
        remap = []
        for f in data["functions"]:
            k = (f["file"], f["name"], f["line"])
            if k not in keys:
                keys[k] = len(fns)
                fns.append(dict(f))  # browser traces add end (last line) and count (exact calls, from coverage)
            remap.append(keys[k])
        for r in data.get("runs", []):
            rid = str(r["id"]) if len(parts) == 1 else f"{pid}.{r['id']}"
            runs.append({**r, "id": rid, "calls": [[remap[c[0]], *c[1:]] for c in r["calls"]]})
    runs.sort(key=lambda r: r["start"])
    edges = _analyse(fns, runs)
    for f in fns:  # sampling misses short calls; coverage counted every one
        f["calls"] = max(f["calls"], f.get("count", 0))
    order = sorted(range(len(fns)), key=lambda i: fns[i]["first"] if fns[i]["first"] is not None else float("inf"))
    new = {old: i for i, old in enumerate(order)}
    for r in runs:
        for c in r["calls"]:
            c[0] = new[c[0]]
    return {"session": session, "live": session == live, "app": app_text(app_info(tdir, session)), "pids": sorted(map(int, parts)),
            "argv": next((d["argv"] for d in parts.values()), []),
            "lost_runs": sum(d.get("lost_runs", 0) for d in parts.values()),
            "running": [f for d in parts.values() for f in d.get("running", [])],
            "functions": [fns[i] for i in order],
            "edges": [[new[a] if a >= 0 else -1, new[b], n] for (a, b), n in edges.items()], "runs": runs}


def _analyse(fns: list[dict], runs: list[dict]) -> dict:
    """Self time per call (its time minus its children's), totals per function, caller -> callee counts."""
    for f in fns:
        f.update(calls=0, raised=0, total_ms=0.0, self_ms=0.0, first=None)
    edges: dict[tuple, int] = {}
    for r in runs:
        calls = r["calls"]
        child = [0.0] * len(calls)
        for c in calls:
            if c[1] >= 0 and c[3] is not None:
                child[c[1]] += c[3]
        for i, c in enumerate(calls):
            fn, parent, t0, ms, flags = c[:5]
            f = fns[fn]
            f["calls"] += 1
            f["raised"] += flags & 1
            at = r["start"] + t0 / 1000
            f["first"] = at if f["first"] is None else min(f["first"], at)
            own = max(0.0, ms - child[i]) if ms is not None else None  # async children may overlap: never < 0
            if ms is not None:
                f["total_ms"] += ms
                f["self_ms"] += own
            c[5:] = [round(own, 3) if own is not None else None]
            e = (calls[parent][0] if parent >= 0 else -1, fn)
            edges[e] = edges.get(e, 0) + 1
    return edges


# ---------- Read the result ----------
def _js_name(label: str) -> str:
    m = re.match(r"^[^(]*?([\w$]+)\s*\(", label) or re.search(r"\bclass\s+([\w$]+)", label)
    return m[1] if m else label


def _defs(text: str, rel: str = ".py") -> list[tuple[int, int, str]]:
    """(first line incl. decorators, last line, qualified name as Python reports it) of every function; for JS/TS,
    (first line, last line, name) of every declaration the outline scanner finds."""
    if posixpath.splitext(rel.lower())[1] in depmap.JS_EXTS:
        return [(s, e, _js_name(label)) for _, s, e, label in navigate.js_decls(text)]
    out = []

    def walk(body, prefix: str) -> None:
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((def_start(n), n.end_lineno, prefix + n.name))
                walk(n.body, f"{prefix}{n.name}.<locals>.")
            elif isinstance(n, ast.ClassDef):
                walk(n.body, f"{prefix}{n.name}.")
    try:
        walk(ast.parse(text).body, "")
    except (SyntaxError, ValueError):
        pass
    return out


def _ran(t: dict) -> list[dict]:
    return [f for f in t["functions"] if f["calls"]]


def _ms(v: float | None) -> str:
    return "running" if v is None else f"{v:,.1f} ms" if v < 10_000 else f"{v / 1000:,.2f} s"


def _pct(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile: the smallest value with at least q of the values at or below it."""
    return sorted_vals[max(0, math.ceil(q * len(sorted_vals)) - 1)]


def entry_stats(t: dict) -> list[dict]:
    """Per entry point (label): runs, median / p95 / max ms, errors, slowest run; busiest first."""
    groups: dict[str, list[dict]] = {}
    for r in t["runs"]:
        groups.setdefault(r["label"], []).append(r)
    out = []
    for label, rs in groups.items():
        done = sorted(r["ms"] for r in rs if r["ms"] is not None)
        slow = max((r for r in rs if r["ms"] is not None), key=lambda r: r["ms"], default=None)
        out.append({"label": label, "runs": len(rs), "errors": sum(1 for r in rs if r["error"]),
                    "median": statistics.median(done) if done else None, "p95": _pct(done, 0.95) if done else None,
                    "max": done[-1] if done else None, "slowest": slow["id"] if slow else None})
    return sorted(out, key=lambda g: -(g["runs"] * (g["median"] or 0)))


# "slower" / "faster" needs this many runs on each side and a median change at least this big (both)
MIN_RUNS, MIN_PCT, MIN_MS = 5, 10.0, 0.5
VERDICTS = ("slower", "faster", "new", "gone", "few runs", "same")  # also the report's order


def _self_per_run(t: dict) -> dict[str, dict[tuple, float]]:
    """Per entry point: each function's self time per finished run (ms), keyed by (file, name)."""
    fs, sums, n = t["functions"], {}, {}
    for r in t["runs"]:
        if r["ms"] is None:
            continue
        n[r["label"]] = n.get(r["label"], 0) + 1
        d = sums.setdefault(r["label"], {})
        for c in r["calls"]:
            if c[5] is not None:
                k = (fs[c[0]]["file"], fs[c[0]]["name"])
                d[k] = d.get(k, 0.0) + c[5]
    return {label: {k: v / n[label] for k, v in d.items()} for label, d in sums.items()}


def compare(root: Path, base: str, new: str) -> dict:
    """Two recordings of one folder side by side, per entry point: runs, median and p95 base -> new, the change,
    a verdict (VERDICTS) and where the time moved (each function's self time per run, biggest changes)."""
    if base == new:
        raise ValueError("Pick two different recordings to compare")
    a, b = load(root, base), load(root, new)
    sa, sb = {g["label"]: g for g in entry_stats(a)}, {g["label"]: g for g in entry_stats(b)}
    pa, pb = _self_per_run(a), _self_per_run(b)
    rows = []
    for label in dict.fromkeys([*sb, *sa]):
        x, y = sa.get(label), sb.get(label)
        row = {"label": label, "base": x, "new": y, "verdict": "new" if not x else "gone" if not y else "few runs"}
        if x and y and x["median"] is not None and y["median"] is not None:
            d = y["median"] - x["median"]
            pct = 100 * d / x["median"] if x["median"] else 0.0
            row.update(delta=round(d, 3), pct=round(pct, 1), p95_delta=round(y["p95"] - x["p95"], 3))
            if min(x["runs"], y["runs"]) >= MIN_RUNS:
                row["verdict"] = ("slower" if d > 0 else "faster") if abs(d) >= MIN_MS and abs(pct) >= MIN_PCT else "same"
        fa, fb = pa.get(label, {}), pb.get(label, {})
        moved = sorted(((k, fa.get(k, 0.0), fb.get(k, 0.0)) for k in fa.keys() | fb.keys()), key=lambda m: -abs(m[2] - m[1]))
        row["moved"] = [{"file": k[0], "name": k[1], "base": round(u, 3), "new": round(v, 3), "delta": round(v - u, 3)}
                        for k, u, v in moved if abs(v - u) >= 0.05][:6]
        rows.append(row)
    rows.sort(key=lambda r: (VERDICTS.index(r["verdict"]), -abs(r.get("delta") or 0)))
    warnings = []
    if a["app"] != b["app"]:
        warnings.append(f"Different app, address or command: {a['app'] or '?'} vs {b['app'] or '?'}")
    if a["live"] or b["live"]:
        warnings.append("A recording is still running: its numbers will change")
    side = lambda t: {"session": t["session"], "app": t["app"], "runs": len(t["runs"])}
    return {"base": side(a), "new": side(b), "rows": rows, "warnings": warnings,
            "rule": {"min_runs": MIN_RUNS, "min_pct": MIN_PCT, "min_ms": MIN_MS}}


def compare_text(c: dict) -> str:
    out = [f"Comparison of {c['base']['session']} ({c['base']['runs']} runs) -> {c['new']['session']} "
           f"({c['new']['runs']} runs), {c['new']['app'] or c['base']['app']}.",
           f"Rule: slower/faster = at least {MIN_RUNS} runs on each side and the median changed by >= {MIN_PCT:.0f}% "
           f"and >= {MIN_MS} ms."] + [f"Warning: {w}" for w in c["warnings"]]
    counts = {v: sum(r["verdict"] == v for r in c["rows"]) for v in VERDICTS}
    out.append("Entry points: " + ", ".join(f"{n} {v}" for v, n in counts.items() if n))
    for r in c["rows"]:
        x, y = r["base"], r["new"]
        line = f"  {r['verdict'].upper():8}  {r['label']}"
        if "delta" in r:
            line += (f"  median {_ms(x['median'])} -> {_ms(y['median'])} ({r['delta']:+.1f} ms, {r['pct']:+.0f}%)"
                     f"  p95 {_ms(x['p95'])} -> {_ms(y['p95'])}  runs {x['runs']} / {y['runs']}")
        elif x or y:
            g = x or y
            line += f"  median {_ms(g['median'])}  runs {g['runs']} (only in the {'base' if x else 'new'} recording)"
        out.append(line)
        if r["moved"] and r["verdict"] in ("slower", "faster", "few runs"):  # new / gone: it is all their own code
            out.append("      time moved (self time per run): " + ", ".join(
                f"{m['file']}:{m['name']} {m['delta']:+.1f} ms ({m['base']:.1f} -> {m['new']:.1f})" for m in r["moved"][:4]))
    return "\n".join(out)


def summary_text(root: Path, t: dict) -> str:
    fns = _ran(t)
    state = " (recording)" if t["live"] else ""
    if not fns:
        return f"Trace {t['session']}{state} of {t['app']}: no project functions ran{' yet' if t['live'] else ''}."
    files = list(dict.fromkeys(f["file"] for f in fns))
    groups = entry_stats(t)
    out = [f"Trace {t['session']}{state} of {t['app']}: {len(t['runs'])} runs of {len(groups)} entry points; {len(fns)} functions "
           f"in {len(files)} files ran (process {', '.join(map(str, t['pids']))})."
           + (f" {t['lost_runs']} older runs dropped (1000-run limit per process)." if t.get("lost_runs") else "")]
    out.append("Entry points (time per run):")
    for g in groups[:30]:
        out.append(f"  {g['label']}  x{g['runs']}  median {_ms(g['median'])}  p95 {_ms(g['p95'])}  max {_ms(g['max'])}"
                   + (f"  errors {g['errors']}" if g["errors"] else "") + f"  (slowest: run {g['slowest']})")
    total = sum(f["self_ms"] for f in fns) or 1.0
    out.append("Where the time goes (self time: inside the function itself, incl. library and I/O waits; all runs):")
    for f in sorted(fns, key=lambda f: -f["self_ms"])[:12]:
        out.append(f"  {_ms(f['self_ms']):>12}  {100 * f['self_ms'] / total:4.0f}%  {f['file']}:{f['name']}  x{f['calls']}"
                   + (f"  raised x{f['raised']}" if f["raised"] else ""))
    by_file: dict[str, float] = {}
    for f in fns:
        by_file[f["file"]] = by_file.get(f["file"], 0.0) + f["self_ms"]
    out.append("By file (self time): " + ", ".join(f"{k} {_ms(v)} ({100 * v / total:.0f}%)"
                                                  for k, v in sorted(by_file.items(), key=lambda kv: -kv[1])[:8]))
    fs = t["functions"]
    cross = [(fs[a], fs[b], n) for a, b, n in t["edges"] if a >= 0 and fs[a]["file"] != fs[b]["file"]]
    if cross:
        out.append("Calls across files:")
        out += [f"  {a['file']}:{a['name']} -> {b['file']}:{b['name']} (x{n})" for a, b, n in cross[:25]]
    unused = []
    for file in files:
        seen = {f["name"] for f in fns + t.get("running", []) if f["file"] == file}  # incl. already running
        names = [q for _, _, q in _defs(read_text(root / file) or "", file) if q not in seen and "<locals>" not in q]
        if names:
            unused.append(f"  {file}: {', '.join(names[:12])}{', ...' if len(names) > 12 else ''}")
    if unused:
        out += ["Not run in these files:"] + unused
    out.append("One run's call timeline: trace show with run=<id>. Source of what ran: get_execution_slice.")
    return "\n".join(out)


def get_run(t: dict, run_id: str) -> dict:
    run = next((r for r in t["runs"] if r["id"] == str(run_id)), None)
    if run is None:
        raise ValueError(f"No run {run_id} in trace {t['session']} (runs: {', '.join(r['id'] for r in t['runs'][:20])}"
                         + (", ..." if len(t["runs"]) > 20 else "") + ")")
    return run


def run_text(t: dict, run_id: str, max_rows: int = 300) -> str:
    """One run as a call timeline: each call's start, time and self time, indented under its caller."""
    r = get_run(t, run_id)
    fs = t["functions"]
    when = time.strftime("%H:%M:%S", time.localtime(r["start"])) + f".{int(r['start'] % 1 * 1000):03d}"
    out = [f"Run {r['id']} of trace {t['session']} ({t['app']}): {r['label']} at {when}, {_ms(r['ms'])}"
           + (f", error: {r['error']}" if r["error"] else "")
           + (f" (called from {r['caller']}, which was already running when recording began)" if r.get("caller") else ""),
           f"inputs: {r['inputs'] or '-'}", f"output: {r['output'] or '-'}",
           "  start ms      time      self  call"]
    kids: dict[int, list[int]] = {}
    for i, c in enumerate(r["calls"]):
        kids.setdefault(c[1], []).append(i)
    stack = [(i, 0) for i in sorted(kids.get(-1, []), key=lambda i: -r["calls"][i][2])]
    rows = 0
    while stack and rows < max_rows:
        i, depth = stack.pop()
        fn, _, t0, ms, flags, own = r["calls"][i]
        f = fs[fn]
        took = "  running" if ms is None else f"{ms:9.1f}"
        mine = "" if own is None else f"{own:9.1f}"
        out.append(f"{t0:10.1f} {took} {mine:>9}  {'  ' * depth}{f['file']}:{f['name']}" + ("  raised" if flags & 1 else ""))
        rows += 1
        stack += [(k, depth + 1) for k in sorted(kids.get(i, []), key=lambda k: -r["calls"][k][2])]
    if len(r["calls"]) > rows:
        out.append(f"... {len(r['calls']) - rows} more calls")
    if r.get("dropped"):
        out.append(f"({r['dropped']} calls beyond the 5000-call limit were counted but not timed)")
    return "\n".join(out)


def execution_slice(root: Path, t: dict, max_chars: int = 24000, run_id: str | None = None) -> str:
    """Source of only the functions that ran (in one run, if given), grouped by file in order of first call."""
    fns = _ran(t)
    what = f"trace {t['session']}"
    if run_id is not None:
        r = get_run(t, run_id)
        first: dict[int, float] = {}
        for c in r["calls"]:
            first.setdefault(c[0], c[2])
        fns = [t["functions"][i] for i in sorted(first, key=first.get)]
        what = f"run {r['id']} ({r['label']}) of trace {t['session']}"
    if not fns:
        return summary_text(root, t)
    files = list(dict.fromkeys(f["file"] for f in fns))
    out = [f"# Execution slice: {what}",
           f"Project: {root.name}. {len(fns)} functions in {len(files)} files ran; only their source is included, "
           "in order of first call."]
    used, skipped = sum(len(s) + 1 for s in out), []
    for file in files:
        text = read_text(root / file)
        if text is None:
            skipped.append(file)
            continue
        lines, defs, taken = text.split("\n"), _defs(text, file), []
        fence = navigate.FENCE.get(posixpath.splitext(file.lower())[1], "")
        sec = ["", f"## {file}"]
        for f in (f for f in fns if f["file"] == file):
            if f.get("end"):  # browser traces know each function's range from coverage
                s, e = f["line"], f["end"]
            else:
                cands = ([d for d in defs if d[2] == f["name"]] or [d for d in defs if d[0] == f["line"]]
                         or [d for d in defs if d[0] <= f["line"] <= d[1]])
                if not cands:
                    skipped.append(f"{file}:{f['name']} (not found; file changed since the trace?)")
                    continue
                s, e, _ = min(cands, key=lambda d: abs(d[0] - f["line"]))
            if any(a <= s and e <= b for a, b in taken):
                continue  # nested in a function already shown
            block = [f"```{fence}  # {f['name']}, lines {s}-{e}, ran x{f['calls']}, self {_ms(f['self_ms'])}"
                     + (", raised" if f["raised"] else ""), *lines[s - 1: e], "```"]
            size = sum(len(x) + 1 for x in block)
            if used + size + sum(len(x) + 1 for x in sec) > max_chars:
                skipped.append(f"{file}:{f['name']}")
                continue
            sec += block
            taken.append((s, e))
        if len(sec) > 2:
            out += sec
            used += sum(len(x) + 1 for x in sec)
    if skipped:
        out += ["", f"(left out to stay under {max_chars} chars or not found: {', '.join(skipped[:20])})"]
    return "\n".join(out)


def run(action: str, root: Path, port: int | None = None, session: str | None = None, run_id: str | None = None,
        url: str | None = None, base: str | None = None) -> str:
    """Text front end shared by the MCP tool and the CLI."""
    if action == "compare":
        if not session or not base:
            raise ValueError("compare needs session (the newer recording) and base (the one to compare against); "
                             "recording ids are listed by trace status")
        return compare_text(compare(root, base, session))
    if action == "enable":
        return enable(root)
    if action == "disable":
        return disable(root)
    if action == "status":
        return status_text(root)
    if action == "start":
        if not port and not url:
            raise ValueError("start needs the port of a running Python app, or the url of a web page")
        return start(root, port, url)
    if action == "stop":
        return summary_text(root, stop(root))
    if action == "show":
        t = load(root, session)
        return run_text(t, run_id) if run_id else summary_text(root, t)
    raise ValueError("action must be enable, start, stop, show, compare, status or disable")
