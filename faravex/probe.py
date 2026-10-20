"""faravex trace probe: runs inside the traced app, loaded at startup by faravex_trace.pth (Enable tracing).

Standalone on purpose: importing the faravex package would switch on its network block inside the app.
Idle until faravex writes <trace dir>/control.json naming this process. Then sys.monitoring times every project
function call and groups calls into runs: a run starts when project code is entered from outside it (a request
handler called by the framework, a job picked up by a worker) and holds everything that call leads to, including
async tasks it starts. Per run it keeps the entry's arguments and result, trimmed and with secrets masked.
Expects ROOT (project folder) and DIR (trace folder) as globals from runpy.run_path.
"""

import atexit
import contextvars
import json
import os
import re
import reprlib
import sys
import threading
import time

MAX_RUNS = 1000   # per recording; the oldest finished runs are dropped first
MAX_CALLS = 5000  # per run; calls beyond it are only counted
SECRET = re.compile(r"(?i)((?:password|passwd|token|secret|api[_-]?key|authorization|cookie)['\"]?\s*[:=]\s*['\"]?)[^'\",\s)}\]&]+")
_short = reprlib.Repr(maxstring=120, maxother=120, maxlist=8, maxdict=8, maxtuple=8, maxset=8, maxlevel=3)
_PLAIN = (str, bytes, int, float, bool, type(None), list, tuple, dict, set, frozenset)


def _write(path, data):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)  # readers never see half a file


def _request(v):
    """("GET /path", "GET /path?query") for an HTTP request object, else None."""
    try:
        if isinstance(getattr(v, "command", None), str) and isinstance(getattr(v, "path", None), str):
            return f"{v.command} {v.path.split('?')[0]}", f"{v.command} {v.path}"  # http.server handler
        if isinstance(getattr(v, "method", None), str) and getattr(v, "url", None) is not None:
            path = getattr(v.url, "path", None) or getattr(v, "path", None)  # Starlette URL / Flask request
            if isinstance(path, str):
                return f"{v.method} {path}", f"{v.method} {v.url}"
    except Exception:
        pass
    return None


def _show(v):
    """Short, masked text for an argument or result. Never calls repr() on arbitrary objects (it can be slow)."""
    try:
        if req := _request(v):
            s = req[1]
        elif isinstance(v, (bytes, bytearray)):  # a request body
            s = _short.repr(bytes(v[:400]).decode("utf-8", "replace"))
        elif isinstance(getattr(v, "status_code", None), int):  # a response
            s = f"<{type(v).__name__} {v.status_code}>"
        elif isinstance(v, _PLAIN) or hasattr(v, "__dataclass_fields__"):
            s = _short.repr(v)
        elif hasattr(v, "model_dump"):  # pydantic model
            s = f"{type(v).__name__}({_short.repr(v.model_dump())})"
        else:
            s = f"<{type(v).__name__}>"
    except Exception:
        s = f"<{type(v).__name__}>"
    return SECRET.sub(r"\1***", s)[:300]


class _Run:
    __slots__ = ("id", "session", "label", "wall", "t0", "t1", "inputs", "output", "error", "calls", "dropped", "open",
                 "caller", "where", "inner", "is_response")

    def __init__(self, rid, session, now, where):
        self.id, self.session, self.wall, self.t0, self.t1, self.where = rid, session, time.time(), now, None, where
        self.label = self.output = self.error = self.caller = self.inner = None
        self.is_response = False
        self.inputs = []  # one entry per entry call (middleware, then the endpoint behind it), then the request body
        self.calls = []  # [code, parent call index or -1, start, end or None, raised, entry, previous context value]
        self.dropped = self.open = 0


def _main(root, tdir):
    mon = sys.monitoring
    E = mon.events
    clock = time.perf_counter
    norm = lambda p: os.path.normcase(os.path.abspath(p))  # noqa: E731
    root = norm(root) + os.sep
    outside = tuple({norm(p) + os.sep for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)})  # the venv, stdlib
    own = norm(__file__)
    control = os.path.join(tdir, "control.json")
    me = os.path.join(tdir, "probes", f"{os.getpid()}.json")
    os.makedirs(os.path.dirname(me), exist_ok=True)
    # (run, call index) of the innermost project call running in this context. Tasks and thread-pool jobs copy the
    # context when they are created, so work a call hands off attaches under that call.
    current = contextvars.ContextVar("faravex_call", default=None)

    ours = {}     # code -> project-relative path, or None for code outside the project
    active = {}   # id(frame) -> (run, call index) for project calls that have not returned
    ambient = set()  # project code seen running above entries
    bodies = {}      # id(frame) of an entry call -> its request objects
    runs = []
    st = {"session": None, "tool": None, "t0": 0.0, "next": 0, "lost": 0}

    def rel(code):
        r = ours.get(code, 0)
        if r == 0:
            if code.co_filename.startswith("<"):  # <frozen ...>, <string>: would resolve inside the cwd
                ours[code] = None
                return None
            f = norm(code.co_filename)
            inside = f.startswith(root) and not f.startswith(outside) and "site-packages" not in f and f != own
            func = code.co_flags & 1 and not code.co_name.startswith("<")  # CO_OPTIMIZED: not a module/class body
            r = ours[code] = f[len(root):].replace(os.sep, "/") if inside and func else None
        return r

    def task():
        aio = sys.modules.get("asyncio")
        try:
            return aio.current_task() if aio else None
        except RuntimeError:  # no event loop in this thread
            return None

    def entry(frame, code, now):
        """A project call with no running project caller: a new run, or part of the run its async task came from."""
        f = frame.f_back
        while f is not None:  # project frames above an entry were running before it (a server loop): not "unused"
            if rel(f.f_code):
                ambient.add(f.f_code)
            f = f.f_back
        t = task()
        here = (threading.get_ident(), id(t) if t is not None else 0)
        came = current.get()
        if came is not None:
            run, parent = came
            # handed-off work: while the run is open, or after it from another thread/task (a response streamed by a
            # worker). A finished run seen again from its own thread is a reused worker serving the next request.
            if run.session == st["session"] and (run.open or run.where != here):
                return run, parent
        st["next"] += 1
        run = _Run(st["next"], st["session"], now, here)
        up = frame.f_back  # project code calling straight in, but started before recording (mid-request start)
        run.caller = f"{rel(up.f_code)}:{up.f_code.co_qualname}" if up is not None and rel(up.f_code) else None
        runs.append(run)
        if len(runs) > MAX_RUNS:
            old = next((r for r in runs if not r.open), runs[0])
            runs.remove(old)
            st["lost"] += 1
        return run, -1

    def note(run, frame, code):
        """An entry call's arguments; the first request seen names the run ("POST /path") so its runs group."""
        names = code.co_varnames[:code.co_argcount + code.co_kwonlyargcount]
        args = [(n, frame.f_locals.get(n)) for n in names]
        req = next((r for _, v in args if (r := _request(v))), None)
        if run.label is None:
            run.label = (req[0] if req else f"{rel(code)}:{code.co_qualname}")[:120]
        text = ", ".join(f"{n}={_show(v)}" for n, v in args if n not in ("self", "cls")) or (req[1] if req else "")
        if text and text not in run.inputs:
            run.inputs.append(text)
        if req:
            bodies[id(frame)] = [v for _, v in args if _request(v)]  # the body is readable once the handler read it

    def on_start(code, offset):
        try:
            if rel(code) is None:
                return mon.DISABLE  # never called again for this function: library code costs nothing
            now = clock()
            frame = sys._getframe(1)
            caller = frame.f_back
            while caller is not None and id(caller) not in active:
                caller = caller.f_back
            if caller is not None:
                run, parent = active[id(caller)]
                is_entry = False
            else:
                run, parent = entry(frame, code, now)
                is_entry = True
            if len(run.calls) >= MAX_CALLS:
                run.dropped += 1
                return
            if is_entry:
                run.open += 1
                note(run, frame, code)
            idx = len(run.calls)
            active[id(frame)] = (run, idx)
            run.calls.append([code, parent, now, None, False, is_entry, current.get()])
            current.set((run, idx))
        except Exception:  # an error here would surface inside the app
            pass

    def finish(code, retval, exc):
        try:
            frame = sys._getframe(2)
            e = active.pop(id(frame), None)
            if e is None:
                return
            run, idx = e
            call = run.calls[idx]
            call[3] = now = clock()
            call[4] = exc is not None
            current.set(call[6])  # back to the caller's value (tasks created after this attach to the caller)
            if call[5]:  # an entry call: the run ends when its last entry call returns
                run.open -= 1
                for req in bodies.pop(id(frame), ()):
                    body = getattr(req, "_body", None)  # Starlette keeps the body once something read it
                    if body and (text := f"body={_show(body)}") not in run.inputs:
                        run.inputs.append(text)
                result = (None if exc is not None else _show(retval),
                          f"{type(exc).__name__}: {exc}"[:300] if exc is not None else None)
                if idx == 0:
                    run.output, run.error = result
                    run.is_response = isinstance(getattr(retval, "status_code", None), int)
                elif call[1] == 0 and (run.inner is None or idx < run.inner[0]):
                    run.inner = (idx, *result)  # the endpoint behind middleware: the first entry directly under it
                if not run.open:
                    run.t1 = now
        except Exception:
            pass

    def on_return(code, offset, retval):
        if rel(code) is None:
            return mon.DISABLE
        finish(code, retval, None)

    def on_resume(code, offset):
        if rel(code) is None:
            return mon.DISABLE
        if id(sys._getframe(1)) not in active:
            ambient.add(code)  # a coroutine/generator that started before recording (a server loop awaiting)

    def on_unwind(code, offset, exc):
        if ours.get(code):  # PY_UNWIND cannot be disabled; skip library code fast
            finish(code, None, exc)

    def outcome(run):
        """Output and error. When the first call is middleware that returned a response object, the endpoint behind
        it says more: its return value, or the exception the framework turned into that response."""
        out, err = run.output, run.error
        if run.inner and (run.is_response or (out is None and err is None)):
            out, err = run.inner[1] or out, run.inner[2] or err
        return {"output": out, "error": err}

    def dump(path, ended):
        idx, fns, out = {}, [], []
        for run in list(runs):
            calls = []
            for code, parent, t0, t1, raised, is_entry, _ in list(run.calls):
                if code not in idx:
                    idx[code] = len(fns)
                    fns.append({"file": rel(code), "name": code.co_qualname, "line": code.co_firstlineno})
                calls.append([idx[code], parent, round((t0 - run.t0) * 1000, 3),
                              round((t1 - t0) * 1000, 3) if t1 is not None else None, int(raised) | 2 * is_entry])
            out.append({"id": run.id, "label": run.label, "start": run.wall,
                        "ms": round((run.t1 - run.t0) * 1000, 3) if run.t1 is not None else None,
                        "inputs": SECRET.sub(r"\1***", " | ".join(run.inputs))[:800], **outcome(run), "dropped": run.dropped,
                        "caller": run.caller,
                        "calls": calls})
        _write(path, {"session": st["session"], "pid": os.getpid(), "argv": sys.argv, "started": st["t0"],
                      "ended": ended, "lost_runs": st["lost"], "functions": fns, "runs": out,
                      "running": [{"file": rel(c), "name": c.co_qualname} for c in list(ambient)]})

    def start(session):
        tool = next((t for t in (2, 3, 4) if mon.get_tool(t) is None), None)
        if tool is None:
            return  # every profiler slot is taken (another profiler or coverage tool runs)
        active.clear()
        runs.clear()
        ambient.clear()
        bodies.clear()
        st.update(session=session, tool=tool, t0=time.time(), next=0, lost=0)
        mon.use_tool_id(tool, "faravex")
        mon.register_callback(tool, E.PY_START, on_start)
        mon.register_callback(tool, E.PY_RETURN, on_return)
        mon.register_callback(tool, E.PY_UNWIND, on_unwind)
        mon.register_callback(tool, E.PY_RESUME, on_resume)
        mon.restart_events()  # re-arm functions switched off during an earlier recording
        mon.set_events(tool, E.PY_START | E.PY_RETURN | E.PY_UNWIND | E.PY_RESUME)
        dump(os.path.join(tdir, f"{session}.{os.getpid()}.live.json"), None)  # tells faravex: recording now

    def stop():
        tool, base = st["tool"], os.path.join(tdir, f"{st['session']}.{os.getpid()}")
        mon.set_events(tool, 0)
        for ev in (E.PY_START, E.PY_RETURN, E.PY_UNWIND, E.PY_RESUME):
            mon.register_callback(tool, ev, None)
        mon.free_tool_id(tool)
        dump(base + ".json", time.time())
        try:
            os.remove(base + ".live.json")
        except OSError:
            pass
        active.clear()
        st.update(session=None, tool=None)

    def loop():
        tick = 0
        while True:
            time.sleep(0.25)
            tick += 1
            try:
                if tick % 8 == 1:  # registry + heartbeat (gone after 6 s silent); written here, not at load,
                    # because runpy swaps sys.argv[0] while the .pth runs
                    _write(me, {"pid": os.getpid(), "ppid": os.getppid(), "argv": sys.argv,
                                "python": sys.version.split()[0]})
                try:
                    with open(control, encoding="utf-8") as fh:
                        c = json.load(fh)
                except (OSError, ValueError):
                    c = {}
                want = c.get("session") if os.getpid() in c.get("pids", ()) else None
                if want != st["session"]:
                    if st["session"]:
                        stop()
                    if want:
                        start(want)
                elif st["session"] and tick % 4 == 0:
                    dump(os.path.join(tdir, f"{st['session']}.{os.getpid()}.live.json"), None)
            except Exception:
                pass  # the trace folder vanished or a write failed: keep the app running, retry next tick

    def at_exit():
        try:
            if st["session"]:
                stop()
            os.remove(me)
        except Exception:
            pass

    atexit.register(at_exit)
    threading.Thread(target=loop, name="faravex-probe", daemon=True).start()


# Once per process: site.py can run the .pth line twice (seen with Windows venvs); two probes would race on files.
if sys.version_info >= (3, 12) and "ROOT" in globals() and "DIR" in globals() and not hasattr(sys, "_faravex_probe"):
    sys._faravex_probe = True
    try:
        _main(ROOT, DIR)  # noqa: F821  (globals from runpy.run_path)
    except Exception:
        pass  # never break the app's startup
