"""Comparing two recordings: verdicts per entry point, where the time moved, warnings; pinned recordings survive
cleanup.

    .venv\\Scripts\\python.exe tests\\test_compare.py      (or pytest, if installed)
"""

import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["FARAVEX_INDEX_DIR"] = str(TMP / "indexes")  # before faravex reads its config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faravex import tracing  # noqa: E402

ROOT = TMP / "app"
ROOT.mkdir()


def record(session: str, entries: dict, cmd: str = "uvicorn app:app") -> None:
    """A recording: per entry point a list of runs, each (run ms, {function: self ms of a call under the entry})."""
    fns, keys, runs = [], {}, []

    def fn(file, name):
        if (file, name) not in keys:
            keys[(file, name)] = len(fns)
            fns.append({"file": file, "name": name, "line": 1})
        return keys[(file, name)]

    for label, rs in entries.items():
        for ms, children in rs:
            calls = [[fn("app.py", label), -1, 0.0, ms, 2]] + [[fn("db.py", k), 0, 0.1, v, 0] for k, v in children.items()]
            runs.append({"id": len(runs) + 1, "label": label, "start": 1000.0 + len(runs), "ms": ms, "inputs": "",
                         "output": "", "error": None, "calls": calls})
    tdir = tracing.trace_dir(ROOT)
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / f"{session}.1.json").write_text(json.dumps({"session": session, "pid": 1, "argv": [], "functions": fns,
                                                        "runs": runs, "lost_runs": 0}))
    (tdir / f"{session}.app.json").write_text(json.dumps({"app": "app", "port": 8000, "cmd": cmd}))


def test_compare_verdicts_and_time_moved():
    six = lambda ms, **kids: [(ms, kids)] * 6
    record("20261020-100000", {"GET /a": six(10, query=6), "GET /b": six(10), "GET /c": [(10, {})] * 2,
                               "GET /e": six(5), "GET /f": six(20, query=12)})
    record("20261020-110000", {"GET /a": six(20, query=16), "GET /b": six(10.4), "GET /c": [(30, {})] * 2,
                               "GET /d": six(7), "GET /f": six(10, query=2)}, cmd="uvicorn app:app --reload")
    c = tracing.compare(ROOT, "20261020-100000", "20261020-110000")
    got = [(r["label"], r["verdict"]) for r in c["rows"]]
    assert got == [("GET /a", "slower"), ("GET /f", "faster"), ("GET /d", "new"), ("GET /e", "gone"),
                   ("GET /c", "few runs"), ("GET /b", "same")], got
    a = c["rows"][0]
    assert (a["delta"], a["pct"], a["base"]["median"], a["new"]["median"]) == (10.0, 100.0, 10.0, 20.0), a
    assert a["moved"][0] == {"file": "db.py", "name": "query", "base": 6.0, "new": 16.0, "delta": 10.0}, a["moved"]
    few = next(r for r in c["rows"] if r["label"] == "GET /c")
    assert few["delta"] == 20.0  # numbers shown, verdict held back: 2 runs a side
    assert any("Different app" in w for w in c["warnings"]), c["warnings"]
    text = tracing.run("compare", ROOT, session="20261020-110000", base="20261020-100000")
    assert "SLOWER    GET /a  median 10.0 ms -> 20.0 ms (+10.0 ms, +100%)" in text, text
    assert "db.py:query +10.0 ms (6.0 -> 16.0)" in text and "Warning: Different app" in text, text
    try:
        tracing.compare(ROOT, "20261020-100000", "20261020-100000")
        raise AssertionError("comparing a recording with itself must fail")
    except ValueError:
        pass


def test_pinned_recordings_survive_cleanup():
    tdir = tracing.trace_dir(ROOT)
    for f in tdir.glob("*"):
        f.unlink()
    for s in ("20261020-090000", "20261020-091000", "20261020-092000", "20261020-093000"):
        record(s, {"GET /a": [(1, {})]})
    tracing.pin(ROOT, "20261020-090000")
    assert tracing.status(ROOT)["pinned"] == ["20261020-090000"]
    old, tracing.KEEP = tracing.KEEP, 2
    try:
        tracing._cleanup(tdir)
    finally:
        tracing.KEEP = old
    assert tracing.status(ROOT)["sessions"] == ["20261020-090000", "20261020-092000", "20261020-093000"]
    tracing.pin(ROOT, "20261020-090000", False)
    assert tracing.status(ROOT)["pinned"] == []


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
