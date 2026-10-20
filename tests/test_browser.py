"""Browser recording: source maps, profile -> runs, and a real page in headless Chrome/Edge (skipped without one).

    .venv\\Scripts\\python.exe tests\\test_browser.py      (or pytest, if installed)
"""

import contextlib
import functools
import http.server
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["FARAVEX_INDEX_DIR"] = str(TMP / "indexes")  # before faravex reads its config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faravex import browser, tracing  # noqa: E402


def test_source_map_lookup():
    m = browser.SourceMap(";;AAAA;AACA,IAAI;AADA")  # line 2 -> src line 0; line 3 -> 1 (col 0 and col 4); line 4 -> 0
    assert m.lookup(0, 0) is None and m.lookup(2, 0) == (0, 0)
    assert m.lookup(3, 0) == (0, 1) and m.lookup(3, 9) == (0, 1) and m.lookup(4, 0) == (0, 0)
    assert m.lookup(99, 0) is None


def test_anonymous_function_names():
    cases = {'  a.addEventListener("click", e => {': 'addEventListener("click") callback (line 7)',
             "el.addEventListener('submit', async function (e) {": "addEventListener('submit') callback (line 7)",
             "items.forEach((item, i) => {": "forEach() callback (line 7)",
             "setInterval(() => fromTs(1), 25);": "setInterval() callback (line 7)",
             "(function () {": "IIFE (line 7)", "(() => {": "IIFE (line 7)", "!function () {": "IIFE (line 7)",
             "export default function () {": "(anonymous, line 7)", "": "(anonymous, line 7)"}
    for text, want in cases.items():
        assert browser.anon_name(text, 7) == want, (text, browser.anon_name(text, 7))


class FakeMapper:  # script "1" is app.js with offsets standing in for lines; anything else is not project code
    def pos(self, sid, line, col):
        return ("app.js", line + 1) if sid == "1" else None

    def offset(self, sid, off):
        return off, 0

    def source_line(self, rel, line):
        return ""


def test_convert_runs_and_counts():
    frame = lambda name, sid="0", line=-1: {"functionName": name, "scriptId": sid, "url": "", "lineNumber": line,
                                            "columnNumber": 0}
    profile = {"startTime": 0, "endTime": 9000, "nodes": [
        {"id": 1, "callFrame": frame("(root)"), "children": [2, 3, 4]},
        {"id": 2, "callFrame": frame("(idle)")}, {"id": 3, "callFrame": frame("(program)")},
        {"id": 4, "callFrame": frame("dispatch", "9", 0), "children": [5]},  # library code calling the project
        {"id": 5, "callFrame": frame("handler", "1", 0), "children": [6]},
        {"id": 6, "callFrame": frame("work", "1", 4)}],
        "samples": [2, 5, 6, 6, 5, 2, 4, 5, 3], "timeDeltas": [1000] * 9}
    coverage = [{"scriptId": "1", "functions": [
        {"functionName": "", "ranges": [{"startOffset": 0, "endOffset": 100, "count": 1}]},  # top level: skipped
        {"functionName": "handler", "ranges": [{"startOffset": 0, "endOffset": 4, "count": 3}]},
        {"functionName": "work", "ranges": [{"startOffset": 4, "endOffset": 9, "count": 7}]},
        {"functionName": "unused", "ranges": [{"startOffset": 9, "endOffset": 12, "count": 0}]}]}]
    d = browser.convert(profile, coverage, FakeMapper(), 1000.0)
    assert d["functions"] == [{"file": "app.js", "name": "handler", "line": 1, "count": 3, "end": 4},
                              {"file": "app.js", "name": "work", "line": 5, "count": 7, "end": 9}], d["functions"]
    r1, r2 = d["runs"]
    assert r1["label"] == "app.js:handler" and abs(r1["start"] - 1000.002) < 1e-9 and r1["ms"] == 4.0, r1
    assert r1["calls"] == [[0, -1, 0.0, 4.0, 2], [1, 0, 1.0, 2.0, 0]], r1["calls"]  # work under handler
    assert r2["calls"] == [[0, -1, 0.0, 1.0, 2]] and r2["id"] == 2, r2  # entered from library code


MAIN_JS = """function work(n) {
  const end = performance.now() + n;
  while (performance.now() < end) {}
  return helper(n);
}
function helper(n) { return n * 2; }
function tick() {
  work(3);
}
setInterval(tick, 20);
"""
UTIL_TS = """function fromTs(n) {
  const end = performance.now() + 2;
  while (performance.now() < end) {}
  return n + 1;
}
setInterval(() => fromTs(1), 25);
"""


def test_reused_script_id_is_remapped():
    """A tab moved to another site gets a new renderer whose script ids restart (Chrome may keep profiling across the
    move): an id that now names another script must not keep the old script's mapping."""
    class Tab:
        events: list = []

        def call(self, method, **params):
            if method == "Profiler.stop":
                return {"profile": {"nodes": [], "startTime": 0, "endTime": 0, "samples": [], "timeDeltas": []}}
            return {"result": []}
    tab = Tab()
    rec = browser._Recorder(TMP, tab, lambda d: None)
    old = {"scriptId": "3", "url": "http://localhost/app/main.js", "executionContextId": 1}
    rec.scripts["3"], rec.mapper.cache["3"] = old, {"map": None, "files": [], "file": "app/main.js", "starts": [0]}
    tab.events = [{"method": "Debugger.scriptParsed", "params": old}]  # same script again: mapping kept
    rec.flush()
    assert "3" in rec.mapper.cache
    tab.events = [{"method": "Debugger.scriptParsed",
                   "params": {"scriptId": "3", "url": "http://127.0.0.1/dist/bundle.js", "executionContextId": 2}}]
    rec.flush()
    assert "3" not in rec.mapper.cache and rec.scripts["3"]["url"].endswith("bundle.js")


def test_browser_end_to_end():
    try:
        browser.find_browser()
    except ValueError:
        print("skip test_browser_end_to_end: no Chrome or Edge")
        return
    root = TMP / "site"
    for rel, text in {
        "index.html": '<!doctype html><script src="app/main.js"></script><script src="/dist/bundle.js"></script>',
        "app/main.js": MAIN_JS, "src/util.ts": UTIL_TS,
        # a "bundle" two lines longer than its source, mapped back to src/util.ts
        "dist/bundle.js": "// generated\n// by hand\n" + UTIL_TS + "//# sourceMappingURL=bundle.js.map\n",
        "dist/bundle.js.map": '{"version":3,"sources":["../src/util.ts"],"mappings":";;AAAA'
                              + ";AACA" * (UTIL_TS.count("\n") - 1) + '"}'}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    browser.ARGS.append("--headless=new")
    try:
        # a static site: a port in a folder without a venv is recorded as a web page
        msg = tracing.start(root, port=srv.server_port)
        assert msg.startswith(f"No Python venv in site, so port {srv.server_port} is recorded as a web page. "
                              "Recording browser trace"), msg
        assert tracing.status(root)["venv"] is False
        live, began = None, time.time()
        while time.time() - began < 3:  # real time: runs show up while recording (one flush a second)
            time.sleep(0.25)
            with contextlib.suppress(ValueError):
                live = tracing.load(root)
            if live and any(r["label"] == "app/main.js:tick" for r in live["runs"]):
                break
        assert live and live["live"] and any(r["label"] == "app/main.js:tick" for r in live["runs"]), live
        print(f"  first live runs after {time.time() - began:.1f} s")
        # Move the tab to another site (localhost -> 127.0.0.1): Chrome gives it a new renderer process, which drops
        # the profiler; the recorder must pick the tab up there.
        port = browser._devtools_port(tracing.trace_dir(root) / "browser-profile")
        tab = next(t for t in browser._http(port, "/json/list") if t["type"] == "page")
        nav = browser._CDP(tab["webSocketDebuggerUrl"])
        nav.call("Page.navigate", url=f"http://127.0.0.1:{srv.server_port}/")
        nav.close()
        moved = time.time()
        time.sleep(3)
        t = tracing.stop(root)
    finally:
        srv.shutdown()
        port = browser._devtools_port(tracing.trace_dir(root) / "browser-profile")
        if port:  # leave no browser running
            browser._CDP(browser._http(port, "/json/version")["webSocketDebuggerUrl"]).call("Browser.close")

    ran = {f["name"]: f for f in t["functions"] if f["calls"]}
    assert {"tick", "work", "helper", "fromTs"} <= set(ran), sorted(ran)
    assert (ran["work"]["file"], ran["work"]["line"], ran["work"]["end"]) == ("app/main.js", 1, 5), ran["work"]
    assert ran["helper"]["calls"] >= 20 and ran["tick"]["calls"] >= 20, (ran["helper"], ran["tick"])  # coverage counts
    assert (ran["fromTs"]["file"], ran["fromTs"]["line"]) == ("src/util.ts", 1), ran["fromTs"]  # through the source map
    assert "setInterval() callback (line 6)" in ran, sorted(ran)  # the nameless arrow, named from its source line
    assert any(r["start"] > moved + 0.5 for r in t["runs"] if r["label"] == "app/main.js:tick"), "lost after the move"
    ids = [int(r["id"]) for r in t["runs"]]
    assert ids == sorted(set(ids)), ids  # flushes merge into one run sequence
    ticks = [r for r in t["runs"] if r["label"] == "app/main.js:tick"]
    fs = t["functions"]
    assert ticks and any(fs[r["calls"][1][0]]["name"] == "work" for r in ticks if len(r["calls"]) > 1), ticks[:3]
    # work() busy-waits 3 ms; sampling every 200 us puts a run's start and end up to one sample late, and a tick
    # running across a once-a-second read is split in two
    timed = sorted(r["ms"] for r in ticks if len(r["calls"]) > 1)
    assert timed[len(timed) // 2] >= 2.0, timed
    assert t["app"] == f"site · browser http://localhost:{srv.server_port}/", t["app"]
    sl = tracing.execution_slice(root, t)
    assert "```javascript  # work, lines 1-5" in sl and "function helper(n)" in sl and "```typescript" in sl, sl
    assert "Not run" not in tracing.summary_text(root, t)  # every declared function ran


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
