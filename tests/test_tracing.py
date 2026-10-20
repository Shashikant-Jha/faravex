"""Live tracing end to end: a real venv, Enable (the .pth), an async app on a port, start/stop, runs, latency,
summary and slices.

    .venv\\Scripts\\python.exe tests\\test_tracing.py      (or pytest, if installed; ~10 s: it creates a venv)
"""

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="cs-trace-"))
os.environ["FARAVEX_INDEX_DIR"] = str(TMP / "indexes")  # before importing faravex: keep test traces out
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faravex import tracing  # noqa: E402

# Shaped like a real async server: a loop that was running before recording starts hands each request to its own
# task; a request does sync work, waits 50 ms, raises and catches, and fans out to child tasks.
APP = '''import asyncio, os, socket, sys, threading
from app import util


async def part(i):
    await asyncio.sleep(0.01)
    return util.helper(i)


async def request(i, token="abc123"):
    util.work(i)
    util.decorated()
    util.slow()
    try:
        util.boom()
    except ValueError:
        pass
    await asyncio.gather(part(1), part(2))
    if os.path.exists("go"):  # created right after trace start returns
        import app.late  # noqa: F401  class body + module body run now, while recording
    return {"ok": i}


async def serve():
    i = 0
    while True:
        i += 1
        await asyncio.create_task(request(i))
        await asyncio.sleep(0.05)


def main():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    probes = sum(t.name == "faravex-probe" for t in threading.enumerate())
    print(s.getsockname()[1], "faravex" in sys.modules, probes, flush=True)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
'''

UTIL = '''import time


def deco(f):
    return f


def work(i):
    return helper(i) * 2


def helper(i):
    return i + 1


@deco
def decorated():
    return 1


def slow():
    time.sleep(0.05)


def boom():
    raise ValueError("expected")


def unused():
    return 0
'''

LATE = '''class Thing:
    def run(self):
        return 1


Thing().run()
'''


def test_port_pid_finds_this_process():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    try:
        assert tracing.port_pid(s.getsockname()[1]) == os.getpid()
    finally:
        s.close()


def test_trace_end_to_end():
    root = TMP / "proj"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("")
    (root / "app" / "main.py").write_text(APP)
    (root / "app" / "util.py").write_text(UTIL)
    (root / "app" / "late.py").write_text(LATE)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")], check=True)
    py = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    msg = tracing.enable(root)
    assert "enabled" in msg and tracing._pth(root).is_file()
    assert tracing._pth(root).relative_to(root).as_posix() in (root / ".gitignore").read_text()

    proc = subprocess.Popen([str(py), "-m", "app.main"], cwd=root, stdout=subprocess.PIPE, text=True)
    try:
        port, imported, probes = proc.stdout.readline().split()
        assert imported == "False", "the probe must not import the faravex package (network block)"
        assert probes == "1", f"{probes} probe threads: the .pth ran more than once"
        deadline = time.time() + 10
        while not tracing._probes(tracing.trace_dir(root)) and time.time() < deadline:
            time.sleep(0.1)
        argv0 = next(iter(tracing._probes(tracing.trace_dir(root)).values()))["argv"][0]
        assert argv0.endswith("main.py"), argv0  # the app's command line, not probe.py
        msg = tracing.start(root, int(port))
        assert "Recording" in msg and "not confirmed" not in msg, msg
        (root / "go").write_text("")  # start returned, so the probe is already recording
        time.sleep(1.5)
        assert tracing.load(root)["live"]
        t = tracing.stop(root)
    finally:
        proc.kill()

    # Functions: what ran, what raised, what was only already running.
    ran = {f["name"]: f for f in t["functions"] if f["calls"]}
    assert {"request", "work", "helper", "decorated", "slow", "boom", "part"} <= set(ran), ran
    assert "unused" not in ran and "serve" not in ran and "main" not in ran  # running before recording
    assert ran["boom"]["raised"] >= 1 and ran["work"]["raised"] == 0
    assert "Thing.run" in ran and "Thing" not in ran  # the method counts; the class body is not a function
    assert {f["file"] for f in t["functions"]} == {"app/main.py", "app/util.py", "app/late.py"}  # no stdlib, <frozen>

    # Runs: one per request, child tasks (gather) inside it, inputs and output kept, secrets masked.
    partial = [r for r in t["runs"] if r.get("caller")]  # recording began mid-request: request was already running
    assert all(r["caller"] in ("app/main.py:request", "app/main.py:part") for r in partial), partial
    runs = [r for r in t["runs"] if r["ms"] is not None and not r.get("caller")]
    assert len(runs) >= 5 and all(r["label"] == "app/main.py:request" for r in runs), [r["label"] for r in runs]
    r = runs[1]
    names = [t["functions"][c[0]]["name"] for c in r["calls"]]
    assert names[0] == "request" and names.count("part") == 2 and "slow" in names, names
    assert r["calls"][names.index("part")][1] == 0  # the gathered task hangs under the request
    assert r["inputs"].startswith("i=") and "token='***'" in r["inputs"] and "abc123" not in r["inputs"], r["inputs"]
    assert r["output"].startswith("{'ok': "), r["output"]
    assert r["ms"] >= 55, r["ms"]  # 50 ms sleep + 10 ms gathered tasks

    # Latency: the 50 ms sleep is the biggest self time; slow's own time covers the sleep it called.
    slow = ran["slow"]
    assert slow["self_ms"] / slow["calls"] >= 45, slow
    assert max(ran.values(), key=lambda f: f["self_ms"])["name"] == "slow", sorted(
        (f["self_ms"], f["name"]) for f in ran.values())
    stats = tracing.entry_stats(t)
    assert stats[0]["label"] == "app/main.py:request" and stats[0]["median"] >= 55, stats

    assert t["app"].startswith(f"{root.name} · :{port} · "), t["app"]  # which app the recording came from
    assert tracing.status(root)["apps"][t["session"]] == t["app"]
    assert tracing._pct([1, 2], 0.95) == 2 and tracing._pct(list(range(1, 21)), 0.95) == 19
    assert tracing.entry_stats({"runs": [{"label": "a", "ms": v, "error": None, "id": str(v)} for v in (1, 3)]})[0]["median"] == 2
    assert tracing._cmd([r"C:\v\Lib\site-packages\uvicorn\__main__.py", "app.main:app"]) == "uvicorn app.main:app"
    text = tracing.summary_text(root, t)
    assert "app/main.py:request  x" in text and "median" in text and "Where the time goes" in text, text
    assert "app/main.py:request -> app/util.py:work" in text and "app/util.py: deco, unused" in text
    assert "app/main.py: main" not in text and "serve" not in text.split("Not run")[-1]  # running, not "not run"
    timeline = tracing.run_text(t, r["id"])
    assert f"Run {r['id']}" in timeline and "app/util.py:slow" in timeline and "  app/main.py:part" in timeline
    sl = tracing.execution_slice(root, t, run_id=r["id"])
    assert "def slow():" in sl and "async def part(i):" in sl and "def unused" not in sl and "Thing" not in sl
    sl = tracing.execution_slice(root, t)
    assert "def helper(i):" in sl and "@deco" in sl and "def unused" not in sl and "def main" not in sl

    assert "disabled" in tracing.disable(root) and not tracing._pth(root).exists()

    # A map of a subfolder uses the repo's venv; the one .pth then points at the subfolder.
    sub = root / "app"
    assert tracing._pth(sub) == tracing._pth(root)
    tracing.enable(root)
    assert tracing._enabled(root) and not tracing._enabled(sub)
    assert "another folder" in tracing.enable(sub) and tracing._enabled(sub) and not tracing._enabled(root)
    assert (root / ".gitignore").read_text().count("faravex_trace.pth") == 1
    tracing.disable(sub)
    # ...and a map of a parent folder finds a venv one level down (an app in backend/).
    (TMP / "repo" / "backend" / ".venv").mkdir(parents=True)
    (TMP / "repo" / "backend" / ".venv" / "pyvenv.cfg").write_text("version = 3.13.0\n")
    assert tracing._venv(TMP / "repo") == TMP / "repo" / "backend" / ".venv"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
