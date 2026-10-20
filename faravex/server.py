"""MCP stdio server exposing semantic code search tools."""

import functools
import json
import logging
import os
import threading
import time
from pathlib import Path

import anyio
from mcp.server.fastmcp import FastMCP

from . import changes, config, navigate, tracing
from .embedder import Embedder
from .index import CodeIndex, Hit

log = logging.getLogger("faravex")

mcp = FastMCP(
    "faravex",
    instructions=(
        "Local code navigation (fully offline). Use these instead of reading whole files: "
        "search_code finds code by meaning or by exact name/string (returns ranked line ranges); "
        "get_symbol_outline shows a file's classes/functions/signatures with line ranges; "
        "get_dependencies shows what a file imports, what imports it, and the docs that describe it; "
        "get_blast_radius lists every file (and test) that a change to a file or symbol can break, "
        "so call it before editing shared code; get_context_slice bundles a file or symbol with the signatures it "
        "depends on, its call sites and doc mentions in one call; search_diff shows what uncommitted changes touch "
        "(usages, similar code, tests to run); trace records which functions a running app actually executes for "
        "one action and get_execution_slice returns only their source. Use grep only to list every occurrence of a "
        "string. "
        "Read only the line ranges "
        "these tools point to. Pass `path` (project root) when the server's working folder is not the project; "
        "absolute file paths also work without it."
    ),
)

_embedder = Embedder()
_indexes: dict[Path, CodeIndex] = {}
_indexes_lock = threading.Lock()


def _get_index(path: str | None) -> CodeIndex:
    root = Path(path).expanduser() if path else config.DEFAULT_ROOT
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    root = root.resolve()
    with _indexes_lock:
        if root not in _indexes:
            _indexes[root] = CodeIndex(root, _embedder)
        return _indexes[root]


def format_hits(hits: list[Hit], max_lines: int) -> str:
    if not hits:
        return "No results."
    out = []
    for n, h in enumerate(hits, 1):
        how = {"both": ", keyword + semantic", "keyword": ", keyword"}.get(h.match, "")
        score = f"score {h.score:.3f}" if h.match != "keyword" or h.score else "exact text"
        out.append(f"[{n}] {h.path}:{h.start_line}-{h.end_line} ({score}{how}){'  ' + h.scope if h.scope else ''}")
        if max_lines > 0:
            lines = h.text.splitlines()
            out.extend(lines[:max_lines])
            if len(lines) > max_lines:
                out.append(f"... ({len(lines) - max_lines} more lines)")
        out.append("")
    return "\n".join(out).rstrip()


def tokens(text: str) -> int:
    """Rough token count: ~4 characters per token (the rule the max_chars budgets use; code runs a bit denser)."""
    return (len(text) + 3) // 4


def _record_call(name: str, kwargs: dict, out: str, ms: float, error: str | None) -> dict:
    """One line per MCP call in the window and in config.CALLS_LOG (shared by every server process) for the UI."""
    args = json.dumps(kwargs, ensure_ascii=False, default=str)
    row = {"at": round(time.time(), 3), "tool": name, "args": args[:300], "ms": round(ms, 1),
           "in": tokens(args), "out": tokens(out), "chars": len(out), "pid": os.getpid()}
    if error:
        row["error"] = error[:300]
    log.info("%s: %.0f ms, ~%d tokens out (%d chars), ~%d in%s", name, ms, row["out"], len(out), row["in"],
             " (error)" if error else "")
    try:
        config.CALLS_LOG.parent.mkdir(parents=True, exist_ok=True)
        with config.CALLS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if config.CALLS_LOG.stat().st_size > config.CALLS_LOG_MAX:
            _trim_calls()
    except OSError:
        log.warning("Could not write %s", config.CALLS_LOG, exc_info=True)
    return row


def _trim_calls() -> None:
    """Keep the newest half of the calls (~4,000 at the default size). A call another process appends while this
    runs can be lost: it is a stats log."""
    lines = config.CALLS_LOG.read_text(encoding="utf-8").splitlines(keepends=True)
    tmp = config.CALLS_LOG.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text("".join(lines[len(lines) // 2:]), encoding="utf-8")
    os.replace(tmp, config.CALLS_LOG)


def tool(fn):
    """Register `fn` as an MCP tool run on a worker thread (tools block on disk, git and the model); `fn` itself
    stays a plain function for the CLI. Each call's time and rough token cost is recorded and heads the result, so
    developers see it in any agent's chat. Plain text, not structured output: a JSON-wrapped string costs the model
    escaped newlines and quotes."""
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        t0, out, error = time.perf_counter(), "", None
        try:
            out = await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except Exception as e:
            error = out = f"{type(e).__name__}: {e}"
            raise
        finally:
            row = _record_call(fn.__name__, kwargs, out, (time.perf_counter() - t0) * 1000, error)
        return f"[faravex: ~{row['out']:,} tokens out, ~{row['in']:,} in, {row['ms']:,.0f} ms]\n{out}"
    mcp.tool(structured_output=False)(run)
    return fn


@tool
def search_code(
    query: str,
    path: str | None = None,
    top_k: int = 8,
    glob: str | None = None,
    max_lines: int = 25,
    refresh: bool = True,
    mode: str = "hybrid",
) -> str:
    """Search a codebase by meaning and by exact text. Returns ranked `file:start-end` hits with code snippets.

    Works for natural-language questions ("where JWT tokens are validated", "retry logic for HTTP requests")
    and for exact names or strings (HTTP_401_UNAUTHORIZED, parseConfig, "connection refused"), or a mix.
    Identifiers, quoted text and partial names (case-insensitive substrings, 3+ characters) are matched literally.

    Args:
        query: Natural-language description of the code you are looking for.
        path: Project root to search. Defaults to the server's working directory.
        top_k: Number of results (1-50).
        glob: Optional comma-separated filename filter, e.g. "*.py" or "src/api/*,*.ts".
        max_lines: Max snippet lines per hit; 0 returns locations only (cheapest).
        refresh: Re-embed files changed since the last call before searching (fast when few changed).
        mode: "hybrid" (default: meaning + exact text, fused), "semantic" (meaning only), or
            "keyword" (exact text only, ranked by BM25; fastest, no model).
    """
    if mode not in ("hybrid", "semantic", "keyword"):
        raise ValueError("mode must be hybrid, semantic or keyword")
    index = _get_index(path)
    note = ""
    if mode == "keyword" and index.is_built:
        refresh = False  # keyword-only stays model-free; changed files are picked up by the next hybrid search
    if refresh or not index.is_built:
        stats = index.update()
        if stats["files_embedded"] or stats["files_removed"]:
            note = f"(index refreshed: {stats['files_embedded']} files re-embedded, {stats['files_removed']} removed)\n"
    hits = index.search(query, top_k=max(1, min(top_k, 50)), glob=glob, mode=mode)
    return note + format_hits(hits, max_lines)


@tool
def index_codebase(path: str | None = None, full: bool = False) -> str:
    """Build or incrementally update the semantic index for a project.

    search_code does this automatically; call explicitly only to pre-build a large repo or
    with full=True to rebuild from scratch.
    """
    index = _get_index(path)
    stats = index.update(full=full)
    return "\n".join(f"{k}: {v}" for k, v in {**stats, **index.status()}.items())


@tool
def index_status(path: str | None = None) -> str:
    """Show index size, location and last update time for a project."""
    status = _get_index(path).status()
    status["model_path"] = str(config.MODEL_PATH)
    status["model_present"] = (config.MODEL_PATH / "config.json").is_file()
    return "\n".join(f"{k}: {v}" for k, v in status.items())


@tool
def get_symbol_outline(file: str, path: str | None = None) -> str:
    """Compact outline of one file: classes, functions/methods with signatures, first docstring line and
    start-end line numbers; markdown headings for docs; top-level keys for JSON/YAML/TOML.

    Costs a fraction of reading the file. Use it to find the line range you need, then read only that range.

    Args:
        file: File path, relative to the project root or absolute.
        path: Project root. Optional when `file` is absolute; defaults to the server's working directory.
    """
    return navigate.outline(*navigate.resolve(file, path))


@tool
def get_dependencies(file: str, path: str | None = None, depth: int = 1) -> str:
    """What a file imports, what imports it, which docs name it or describe it, and its external packages.

    Import edges are read from the code (Python, JS/TS, HTML, CSS); doc links from markdown/specs;
    "semantically linked" from the search index. Files marked [test] are tests.

    Args:
        file: File path, relative to the project root or absolute.
        path: Project root. Optional when `file` is absolute; defaults to the server's working directory.
        depth: Hops to follow in both directions (1 = direct only, max 10).
    """
    return navigate.dependencies(*navigate.resolve(file, path), depth)


@tool
def get_blast_radius(file: str, symbol: str | None = None, path: str | None = None, depth: int = 3) -> str:
    """Everything a change to `file` can break: files importing it directly and transitively (by hop count),
    tests among them, and docs that describe it. Call this before changing shared code.

    With `symbol` (a function, class or constant defined in `file`) it also lists the exact lines in those
    files that name the symbol, and the direct importers that never use it.

    Args:
        file: File path, relative to the project root or absolute.
        symbol: Optional name defined in the file, e.g. "embed_query".
        path: Project root. Optional when `file` is absolute; defaults to the server's working directory.
        depth: Max import hops to follow upward (1-10).
    """
    return navigate.blast_radius(*navigate.resolve(file, path), symbol, depth)


@tool
def get_context_slice(file: str, symbol: str | None = None, path: str | None = None, max_chars: int = 24000) -> str:
    """Self-contained bundle for working on one file or symbol: its source, the signatures of the files it
    imports, the lines in other files that use it, and the doc lines that mention it.

    One call instead of reading several files. Sections are dropped (and listed) to stay under `max_chars`.

    Args:
        file: File path, relative to the project root or absolute.
        symbol: Optional function/class/method in `file`; then only its source is included.
        path: Project root. Optional when `file` is absolute; defaults to the server's working directory.
        max_chars: Output budget (2,000-200,000 characters; ~4 characters per token).
    """
    return navigate.context_slice(*navigate.resolve(file, path), symbol, max_chars)


@tool
def search_diff(path: str | None = None, staged: bool = False, base: str | None = None, top_k: int = 5) -> str:
    """What your uncommitted changes touch, before you commit or to check an edit: for each changed region,
    the function/class it is in, the lines in other files that use the changed names, the most similar code
    in other files, and the tests likely affected (through imports, usages or similarity).

    Args:
        path: Project root (a git repository or a folder inside one). Defaults to the server's working directory.
        staged: Only staged changes (git diff --cached) instead of the working tree plus untracked files.
        base: Compare the working tree with this git ref instead, e.g. "main" or "HEAD~3".
        top_k: Similar-code hits per change (1-20).
    """
    index = _get_index(path)
    return changes.format_text(changes.analyse(index.root, index, staged=staged, base=base, top_k=max(1, min(top_k, 20))))


@tool
def trace(action: str, port: int | None = None, path: str | None = None, session: str | None = None,
          run: str | None = None, url: str | None = None, base: str | None = None) -> str:
    """Live execution tracing and latency of a running Python 3.12+ app, or of the JS/TS a web page runs: which
    project functions actually run for each request or browser task (run), how long each took, and where the time
    goes.

    Actions:
      enable  - Python apps, once per project: the app's venv loads the faravex probe at startup (restart the app).
      start   - with `port`: record the Python app listening on it; exercise it (e.g. call its endpoints). In a folder
                without a venv (static site, JS dev server) the port's page is recorded in the browser instead.
                With `url`: open that page in Chrome/Edge (faravex's own browser profile, reused across recordings)
                and record its tab; the user then uses the page in that window. Needs no enable. Source maps (Vite,
                webpack dev builds) map bundled or TypeScript code back to project files.
      stop    - stop recording; returns the latency report: per entry point (e.g. "POST /api/orders") the runs and
                median / p95 / max time, the functions and files where the time goes (self time, which includes
                library and I/O waits inside them), calls across files, and functions in those files that did not run.
      show    - the latest trace (or `session`); while recording, what ran so far. With `run`, that one run's call
                timeline: every call's start, time and self time under its caller, plus the run's inputs and output.
      compare - two recordings of the same folder side by side (`session` = newer, `base` = the one to compare
                against): per entry point median and p95 base -> new, the change, a verdict (slower / faster / same,
                or new / gone / few runs) and which functions the time moved to. Workflow for a code change: record
                the actions, change the code, record the same actions again, compare.
      status  - whether tracing is enabled, which processes have the probe, whether a trace is recording, and the
                kept recordings (ids for show / compare; pinned ones are never cleaned up).
      disable - remove the probe from the venv.
    Then get_execution_slice gives the source of only the functions that ran.

    Args:
        action: enable | start | stop | show | compare | status | disable.
        port: Port the Python app listens on (start).
        path: Project root of the traced app. Defaults to the server's working directory.
        session: Trace id for show, e.g. "20261011-142501" (default: latest); for compare, the newer recording.
        run: Run id for show (from the report's "slowest: run N").
        url: Page to record in the browser (start), e.g. "http://localhost:5173/".
        base: For compare, the recording to compare `session` against.
    """
    return tracing.run(action, tracing.project_root(path), port, session, run, url, base)


@tool
def get_execution_slice(path: str | None = None, session: str | None = None, max_chars: int = 24000,
                        run: str | None = None) -> str:
    """Source of only the functions that ran in a trace (see `trace`), or in one run of it, grouped by file in order
    of first call, each with its call count and self time: the code behind one action without the code around it.

    Args:
        path: Project root of the traced app. Defaults to the server's working directory.
        session: Trace id (default: latest, or the one recording).
        max_chars: Output budget (2,000-200,000 characters; ~4 characters per token).
        run: Only the functions of this run (a run id from the trace report).
    """
    root = tracing.project_root(path)
    return tracing.execution_slice(root, tracing.load(root, session), max(2000, min(max_chars, 200_000)), run)


def _exit_with_parent() -> None:
    """Exit as soon as the parent dies. Claude Code may kill the venv launcher without closing stdin, which would
    leave this interpreter (and the model on the GPU) running."""
    ppid = os.getppid()
    if os.name == "nt":
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x00100000, False, ppid)  # SYNCHRONIZE
        if handle:
            k32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
    else:
        while os.getppid() == ppid:
            time.sleep(2)
    log.info("Parent process %d exited; shutting down", ppid)
    os._exit(0)


def serve() -> None:
    threading.Thread(target=_exit_with_parent, name="parent-watchdog", daemon=True).start()
    _embedder.warmup()
    mcp.run(transport="stdio")


def serve_http(port: int | None = None, ui_port: int | None = None, open_browser: bool = False) -> None:
    """MCP over streamable HTTP at http://127.0.0.1:<port>/mcp plus the browser UI, in one process that every
    client shares. Listens at once; the model loads in the background (nothing reads stdin here, so the Windows
    DLL deadlock that pins stdio warmup to the main thread can't happen). Early tool calls wait for it."""
    from . import web

    threading.Thread(target=web.serve, args=(ui_port, open_browser), name="ui", daemon=True).start()
    threading.Thread(target=_embedder.warmup, name="warmup", daemon=True).start()
    mcp.settings.port = port or config.MCP_PORT
    log.info("faravex MCP on http://127.0.0.1:%d/mcp", mcp.settings.port)
    mcp.run(transport="streamable-http")
