"""Every MCP tool call is logged with its time and rough token cost, and the UI's /api/calls totals them per tool.

    .venv\\Scripts\\python.exe tests\\test_calls.py      (or pytest, if installed)
"""

import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
os.environ["FARAVEX_INDEX_DIR"] = str(TMP / "indexes")  # before faravex reads its config
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anyio  # noqa: E402

from faravex import config, server, web  # noqa: E402


def test_calls_logged_and_totalled():
    res = anyio.run(server.mcp.call_tool, "index_status", {"path": str(TMP)})
    assert isinstance(res, list) and len(res) == 1, res  # plain text content, no JSON-wrapped structured copy
    head, body = res[0].text.split("\n", 1)
    assert head.startswith("[faravex: ~") and body.startswith("root: "), res[0].text
    try:
        anyio.run(server.mcp.call_tool, "index_status", {"path": str(TMP / "missing")})
    except Exception:
        pass  # the failing call is still logged
    r = web.calls()
    assert r["count"] == 2 and r["log"] == str(config.CALLS_LOG), r
    bad, ok = r["calls"]  # newest first
    assert ok["tool"] == "index_status" and "error" not in ok and ok["out"] == (ok["chars"] + 3) // 4 > 0, ok
    assert ok["in"] == server.tokens(ok["args"]) and str(TMP).replace("\\", "\\\\") in ok["args"], ok
    assert head == f"[faravex: ~{ok['out']:,} tokens out, ~{ok['in']:,} in, {ok['ms']:,.0f} ms]", head
    assert ok["chars"] == len(body), ok
    assert "Not a directory" in bad["error"], bad
    assert r["tools"] == [{"tool": "index_status", "calls": 2, "in": ok["in"] + bad["in"],
                           "out": ok["out"] + bad["out"], "ms": ok["ms"] + bad["ms"]}], r["tools"]


def test_log_trimmed_to_newest_half():
    old, config.CALLS_LOG_MAX = config.CALLS_LOG_MAX, 2000
    try:
        for i in range(100):
            server._record_call("search_code", {"query": f"q{i}"}, "x" * 40, 1.0, None)
        assert config.CALLS_LOG.stat().st_size <= 2000, config.CALLS_LOG.stat().st_size
        rows = web.calls()["calls"]
        assert '"q99"' in rows[0]["args"] and len(rows) >= 5, rows  # newest kept, not emptied
    finally:
        config.CALLS_LOG_MAX = old


if __name__ == "__main__":
    test_calls_logged_and_totalled()
    test_log_trimmed_to_newest_half()
    print("ok")
