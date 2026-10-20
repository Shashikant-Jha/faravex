"""The browser UI fits laptop and desktop windows: no sideways page scroll, no control off-screen, at 768-2560 px
wide, in every view (map + side panel, Calls, Record, folder picker), with long paths, names and status text.
Headless Chrome/Edge; skipped without one.

    .venv\\Scripts\\python.exe tests\\test_layout.py      (or pytest, if installed)
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["FARAVEX_INDEX_DIR"] = str(TMP / "indexes")  # before faravex reads its config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faravex import browser, config, tracing, web  # noqa: E402

SIZES = [(768, 700), (960, 600), (1024, 640), (1366, 768), (1920, 1080), (2560, 1440)]
LONG = "a-rather-long-folder-name-that-keeps-going"
# Visible elements that end past the window's right edge, outermost only; the map canvas pans and the code preview
# scrolls by design.
AUDIT = r"""(() => {
  const vw = document.documentElement.clientWidth, out = [];
  const shown = (el) => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
  for (const el of document.querySelectorAll('body *')) {
    if (!shown(el) || el.closest('[data-map], .code, dialog:not([open])')) continue;
    const r = el.getBoundingClientRect(), p = el.parentElement && el.parentElement.getBoundingClientRect();
    if (r.width && r.right > vw + 1 && !(p && p.right > vw + 1))
      out.push(`${el.tagName.toLowerCase()}.${el.className} "${(el.textContent || '').trim().slice(0, 30)}" ends at ${Math.round(r.right)}`);
  }
  return {sideways: document.documentElement.scrollWidth > vw, off: out.slice(0, 5)};
})()"""


def make_project() -> Path:
    root = TMP / "project"
    deep = root / LONG / LONG / "static" / "js"
    deep.mkdir(parents=True)
    (deep / "component-with-a-really-long-file-name.js").write_text("export function render() { return 1; }\n")
    (root / "main.py").write_text(f"from {LONG.replace('-', '_')} import x\n")
    (root / "README.md").write_text(f"See `{LONG}/{LONG}/static/js/component-with-a-really-long-file-name.js`.\n")
    rel = f"{LONG}/{LONG}/static/js/component-with-a-really-long-file-name.js"
    tdir = tracing.trace_dir(root)  # one recorded run with long names, as the Record view shows them
    tdir.mkdir(parents=True)
    fns = [{"file": rel, "name": 'addEventListener("click") callback (line 1234)', "line": 1, "end": 1},
           {"file": rel, "name": "aFunctionWithAnExtraordinarilyLongDescriptiveName", "line": 1, "end": 1}]
    run = {"id": 1, "label": f'{rel}:addEventListener("click") callback (line 1234)', "start": time.time(), "ms": 4.2,
           "inputs": "event=" + "x" * 300, "output": "", "error": None, "dropped": 0, "caller": None,
           "calls": [[0, -1, 0.0, 4.2, 2], [1, 0, 0.5, 3.0, 0]]}
    for session, ms in (("20261020-110000", 2.1), ("20261020-120000", 4.2)):  # two, so the Record view can compare
        r = {**run, "ms": ms, "calls": [[0, -1, 0.0, ms, 2], [1, 0, 0.5, ms * 0.7, 0]]}
        (tdir / f"{session}.1.json").write_text(json.dumps({"session": session, "pid": 1, "argv": [],
                                                            "functions": fns, "runs": [r] * 6, "lost_runs": 0}))
        (tdir / f"{session}.app.json").write_text(json.dumps({"app": "project", "port": None,
                                                              "cmd": "browser http://localhost:5173/" + LONG}))
    config.CALLS_LOG.parent.mkdir(parents=True, exist_ok=True)
    config.CALLS_LOG.write_text(json.dumps({"at": time.time(), "tool": "get_context_slice", "args": json.dumps(
        {"file": rel, "path": str(root)}), "ms": 12.0, "in": 40, "out": 900, "chars": 3600}) + "\n")
    return root


def test_layout_fits():
    try:
        exe = browser.find_browser()
    except ValueError:
        print("skip test_layout_fits: no Chrome or Edge")
        return
    root = make_project()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    threading.Thread(target=web.serve, args=(port, False), daemon=True).start()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dport = s.getsockname()[1]
    proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={dport}",
                             f"--user-data-dir={TMP / 'chrome'}", "about:blank"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    problems = []
    try:
        for _ in range(100):
            try:
                tab = next(t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{dport}/json"))
                           if t["type"] == "page")
                break
            except (OSError, StopIteration):
                time.sleep(0.1)
        c = browser._CDP(tab["webSocketDebuggerUrl"])
        ev = lambda js: c.call("Runtime.evaluate", expression=js, returnByValue=True, awaitPromise=True)["result"].get("value")
        c.call("Page.navigate", url=f"http://127.0.0.1:{port}/")
        time.sleep(1)
        ev("localStorage.setItem('faravex.guided', '1')")  # no tour on top of the layout
        for w, h in SIZES:
            c.call("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=1, mobile=False)
            c.call("Page.reload")
            time.sleep(1)
            ev(f"faravex.mapFolder({json.dumps(str(root))})")
            time.sleep(1.5)
            ev("faravex.status('Read 3 files and 2 links; ' + 'a long status message that keeps going '.repeat(6))")
            views = {"map + side panel": f"faravex.select({json.dumps(f'{LONG}/{LONG}/static/js/component-with-a-really-long-file-name.js')})",
                     "calls": "faravex.setView('calls')",
                     "record": "faravex.setView('record'); document.querySelector('.card-head')?.click()",
                     "record + compare": "(() => { const s = document.querySelector('[data-compare]');"
                                         " s.value = s.options[1]?.value || ''; s.dispatchEvent(new Event('change'));"
                                         " setTimeout(() => document.querySelector('.cmp-row')?.click(), 300); })()",
                     "folder picker": "document.querySelector('[data-browse]').click()"}
            for name, js in views.items():
                ev(js)
                time.sleep(0.8)
                a = ev(AUDIT)
                if a["sideways"] or a["off"]:
                    problems.append(f"{w}x{h} {name}: sideways={a['sideways']} {a['off']}")
                if name == "record + compare" and not ev("!!document.querySelector('.cmp-row.v-slower + .cmp-moved:not([hidden])')"):
                    problems.append(f"{w}x{h} {name}: no comparison (slower, time moved open) shown")
            ev("document.querySelector('[data-picker]').close(); faravex.setView('map')")
        # Big screens are zoomed one step: clicks must still land on the map node under the pointer.
        node = ev("(() => { const r = document.querySelector('.node rect').getBoundingClientRect();"
                  " return [r.left + r.width / 2, r.top + r.height / 2]; })()")
        for kind in ("mousePressed", "mouseReleased"):
            c.call("Input.dispatchMouseEvent", type=kind, x=node[0], y=node[1], button="left", clickCount=1)
        time.sleep(0.5)
        assert ev("!!document.querySelector('.node.sel')"), "a click at 2560 px did not select the node under it"
    finally:
        proc.kill()
    assert not problems, "\n".join(problems)


if __name__ == "__main__":
    test_layout_fits()
    print("ok test_layout_fits")
