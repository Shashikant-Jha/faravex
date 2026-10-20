"""Structure tools for agents: file outlines and the import graph (who imports what, what breaks on a change).

Plain-text output, kept compact: an agent reads these instead of whole files.
"""

import ast
import difflib
import io
import json
import posixpath
import re
import threading
import tokenize
from collections import defaultdict
from pathlib import Path

from . import config, depmap
from .files import def_start, iter_source_files, md_headings, read_text
from .index import index_dir_for

MAX_OUTLINE_LINES = 400
GRAPH_EXTS = {".py", ".html", ".htm", ".css", ".scss", ".sass", ".less"} | depmap.JS_EXTS
TEST_RX = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.[^/]+$|\.(test|spec)\.[^/]+$", re.I)


def _clip(s: str, n: int = 160) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "..."


def usage_lines(rel: str, text: str, name: str) -> list[int]:
    """Lines that use `name` as code, so a common word ("docs") in prose, strings or comments does not count:
    identifier tokens in Python; JS/TS outside strings, comments and regexes; `backticked` code in docs; whole
    words elsewhere."""
    ext = posixpath.splitext(rel.lower())[1]
    if ext == ".py":
        try:
            return sorted({t.start[0] for t in tokenize.generate_tokens(io.StringIO(text).readline)
                           if t.type == tokenize.NAME and t.string == name})
        except (tokenize.TokenError, SyntaxError):  # broken file: whole words below
            pass
    if ext in depmap.JS_EXTS:
        chars = list(text)
        for s, e in _js_scan(text)[1]:
            chars[s:e] = [ch if ch == "\n" else " " for ch in chars[s:e]]  # newlines kept: line numbers stay
        text = "".join(chars)
    lines = text.split("\n")
    if depmap.kind_of(rel) == "doc":
        lines = [" ".join(re.findall(r"`([^`]+)`", s)) for s in lines]
    # shortcut: HTML and other languages match whole words, text included; add a scanner per language if that is noisy
    rx = re.compile(rf"(?<![\w$]){re.escape(name)}(?![\w$])")  # $ is a word character in JS
    return [i for i, s in enumerate(lines, 1) if rx.search(s)]


# ---------- Paths ----------
def _git_root(p: Path) -> Path | None:
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return d
    return None


def resolve(file: str, path: str | None) -> tuple[Path, str]:
    """(project root, posix path relative to it). Absolute files work without `path`: the root is then the
    enclosing git repo, or the server's default root if the file is inside it, else the file's folder."""
    f = Path(file.strip().strip('"')).expanduser()
    if path:
        root = Path(path).expanduser().resolve()
    elif f.is_absolute():
        fr, default = f.resolve(), config.DEFAULT_ROOT.resolve()
        root = _git_root(fr.parent) or (default if fr.is_relative_to(default) else fr.parent)
    else:
        root = config.DEFAULT_ROOT.resolve()
    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")
    if f.is_absolute():
        try:
            rel = f.resolve().relative_to(root).as_posix()
        except ValueError:
            raise ValueError(f"{f} is not inside the project {root}") from None
    else:
        rel = posixpath.normpath(file.strip().replace("\\", "/"))
        if rel.startswith("../") or rel == "..":
            raise ValueError(f"{file} is outside the project {root}")
    return root, rel


# ---------- Outline ----------
def outline(root: Path, rel: str) -> str:
    abspath = root / rel
    if not abspath.is_file():
        raise ValueError(f"No such file: {rel} (project {root})")
    text = read_text(abspath)
    if text is None:
        raise ValueError(f"{rel} is binary or too large to outline")
    ext = posixpath.splitext(rel.lower())[1]
    n = len(text.splitlines())
    head = f"{rel} ({n} lines)"
    if ext == ".py":
        try:
            body, doc = _py_outline(text)
            head += f" - {doc}" if doc else ""
        except (SyntaxError, ValueError) as e:
            body = [f"(Python parse failed: {e}; showing a heuristic outline)"] + _generic_outline(text)
    elif ext in depmap.JS_EXTS:
        body = _js_outline(text)
    elif ext in depmap.DOC_EXTS:
        body = _md_outline(text)
    elif ext in (".json", ".yaml", ".yml", ".toml"):
        body = _config_outline(text, ext)
    else:
        body = _generic_outline(text)
    if not body:
        body = ["(no declarations found)"]
    if len(body) > MAX_OUTLINE_LINES:
        body = body[:MAX_OUTLINE_LINES] + [f"... {len(body) - MAX_OUTLINE_LINES} more entries"]
    return "\n".join([head, *body])


def _py_outline(text: str) -> tuple[list[str], str]:
    tree = ast.parse(text)
    out: list[str] = []
    consts: list[str] = []

    def doc1(n) -> str:
        d = ast.get_docstring(n)
        return _clip(d.strip().splitlines()[0], 100) if d and d.strip() else ""

    def emit(n, indent: int, label: str) -> None:
        deco = " ".join("@" + _clip(ast.unparse(d), 40) for d in getattr(n, "decorator_list", []))
        d = doc1(n)
        out.append(f"{'  ' * indent}{n.lineno}-{n.end_lineno}  {deco + ' ' if deco else ''}{label}{'  - ' + d if d else ''}")

    def walk(body, indent: int) -> None:
        fields, first = [], len(out)
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ret = f" -> {ast.unparse(n.returns)}" if n.returns else ""
                pre = "async def" if isinstance(n, ast.AsyncFunctionDef) else "def"
                emit(n, indent, _clip(f"{pre} {n.name}({ast.unparse(n.args)}){ret}"))
            elif isinstance(n, ast.ClassDef):
                bases = ", ".join(ast.unparse(b) for b in [*n.bases, *n.keywords])
                emit(n, indent, f"class {n.name}({bases})" if bases else f"class {n.name}")
                walk(n.body, indent + 1)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                (fields if indent else consts).append(f"{n.target.id}: {_clip(ast.unparse(n.annotation), 40)}")
            elif isinstance(n, ast.Assign) and indent == 0:
                consts.extend(t.id for t in n.targets if isinstance(t, ast.Name) and (t.id.isupper() or t.id == "__all__"))
            elif isinstance(n, ast.If) and indent == 0 and "__main__" not in ast.unparse(n.test):
                walk(n.body + n.orelse, indent)
            elif isinstance(n, ast.Try) and indent == 0:
                walk(n.body + [s for h in n.handlers for s in h.body] + n.orelse, indent)
        if fields:  # right under the class line
            out.insert(first, f"{'  ' * indent}fields: {_clip(', '.join(fields), 300)}")

    walk(tree.body, 0)
    if consts:
        out.insert(0, f"module-level: {_clip(', '.join(consts), 300)}")
    return out, doc1(tree)


# JS / TS: a small scanner that skips strings, comments, template literals and regex literals, so brace depth
# (and with it each declaration's end line) is right without a real parser.
RX_JS_DECL = [
    ("fn", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([\w$]+)\s*(\([^)]*\)?)")),
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([\w$]+)([^{]*)")),
    ("fn", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([\w$]+)\s*(?::[^=]+)?=\s*(?:async\s+)?(\([^)]*\)|[\w$]+)\s*(?::[^=]+)?=>")),
    ("fn", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([\w$]+)\s*=\s*(?:async\s+)?function\b\s*\*?\s*(\([^)]*\)?)")),
    ("type", re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(interface|type|enum)\s+([\w$]+)")),
]
RX_JS_METHOD = re.compile(r"^\s*(?:(?:public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*(#?[\w$]+)\s*(\([^)]*\)?)\s*(?::[^{]+)?\{")
JS_NOT_METHOD = {"if", "for", "while", "switch", "catch", "function", "return", "with", "else", "do", "try"}
REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {""}


def _js_scan(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Code braces as (line_no, +1 open / -1 close) events in order, and the (start, end) offsets skipped as
    strings, comments, template text and regexes (braces there are not code)."""
    events: list[tuple[int, int]] = []
    skips: list[tuple[int, int]] = []
    i, n, line = 0, len(text), 1
    prev = ""  # last significant character, to tell a regex literal from a division
    stack: list[str] = []  # "{" or "${" (template expression)
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1; i += 1; continue
        if c in " \t\r":
            i += 1; continue
        two = text[i:i + 2]
        if two == "//":
            j = text.find("\n", i); j = n if j < 0 else j
            skips.append((i, j)); i = j; continue
        if two == "/*":
            j = text.find("*/", i + 2); j = n if j < 0 else j + 2
            skips.append((i, j)); line += text.count("\n", i, j); i = j; continue
        if c in "'\"":
            j = i + 1
            while j < n and text[j] not in (c, "\n"):
                j += 2 if text[j] == "\\" else 1
            skips.append((i, j)); i = j + 1; prev = c; continue
        if c == "`" or (c == "}" and stack and stack[-1] == "${"):
            if c == "}":
                stack.pop()
            j = i + 1
            while j < n and text[j] != "`":
                if text[j] == "\\":
                    j += 2; continue
                if text.startswith("${", j):
                    break
                j += 1
            skips.append((i, j)); line += text.count("\n", i, j)
            if j < n and text[j] != "`":
                stack.append("${"); i = j + 2; prev = "{"; continue
            i = j + 1; prev = "`"; continue
        if c == "/" and prev in REGEX_BEFORE:
            j, cls = i + 1, False
            while j < n and text[j] != "\n":
                if text[j] == "\\":
                    j += 2; continue
                if text[j] == "[":
                    cls = True
                elif text[j] == "]":
                    cls = False
                elif text[j] == "/" and not cls:
                    break
                j += 1
            skips.append((i, j)); i = j + 1; prev = "/r"; continue
        if c == "{":
            stack.append("{"); events.append((line, 1))
        elif c == "}" and stack:
            stack.pop(); events.append((line, -1))
        if c.isalnum() or c in "_$":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            word = text[i:j]
            prev = "" if word in ("return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw") else "a"
            i = j; continue
        prev = c; i += 1
    return events, skips


def js_decls(text: str) -> list[list]:
    """[depth, start_line, end_line, label] per declaration; depth counts enclosing classes."""
    lines = text.splitlines()
    events = _js_scan(text)[0]
    by_line: dict[int, list[int]] = defaultdict(list)
    for ln, d in events:
        by_line[ln].append(d)
    out: list[str] = []
    frames: list[list] = []  # one per open brace: [kind, index into out or None]
    for ln, src in enumerate(lines, 1):
        inside_fn = any(k == "fn" for k, _ in frames)
        in_class = bool(frames) and frames[-1][0] == "class"
        found = None
        if not inside_fn:
            for kind, rx in RX_JS_DECL:
                m = rx.match(src)
                if m:
                    if kind == "type":
                        found = ("type", f"{m.group(1)} {m.group(2)}")
                    elif kind == "class":
                        found = ("class", _clip(f"class {m.group(1)}{m.group(2).rstrip()}"))
                    else:
                        params = m.group(2) if m.group(2).startswith("(") else f"({m.group(2)})"  # `x => ...`
                        found = ("fn", _clip(f"{m.group(1)}{params}"))
                    break
            if not found and in_class:
                m = RX_JS_METHOD.match(src)
                if m and m.group(1) not in JS_NOT_METHOD:
                    found = ("fn", _clip(f"{m.group(1)}{m.group(2)}"))
        slot = None
        if found:
            out.append([sum(1 for k, _ in frames if k == "class"), ln, ln, found[1]])
            slot = len(out) - 1
        pushed = []
        for d in by_line.get(ln, []):
            if d > 0:
                frames.append(["block", None]); pushed.append(frames[-1])
            elif frames:
                kind, idx = frames.pop()
                if idx is not None:
                    out[idx][2] = ln
        if slot is not None:
            # The body is the first brace opened on this line that is still open (not a `= {}` default).
            body = next((f for f in pushed if any(f is x for x in frames)), None)
            if body is not None:
                body[0], body[1] = (found[0] if found[0] != "type" else "block"), slot
    return out


def _js_outline(text: str) -> list[str]:
    return [f"{'  ' * d}{s}-{e}  {label}" for d, s, e, label in js_decls(text)]


def _md_outline(text: str) -> list[str]:
    return [f"{'  ' * (level - 1)}{ln}  {_clip(title, 120)}" for ln, level, title in md_headings(text.splitlines())]


def _config_outline(text: str, ext: str) -> list[str]:
    if ext == ".json":
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return [f"keys: {_clip(', '.join(map(str, data)), 600)}"]
        except ValueError:
            pass
        return []
    rx = re.compile(r"^\[\[?([^\]]+)\]\]?\s*$") if ext == ".toml" else re.compile(r"^([\w.-]+)\s*:")
    return [f"{ln}  {m.group(1)}" for ln, s in enumerate(text.splitlines(), 1) if (m := rx.match(s))]


RX_GENERIC = re.compile(
    r"^\s{0,8}(?:(?:pub(?:\([^)]*\))?|public|private|protected|internal|static|final|abstract|sealed|override|virtual|"
    r"async|export|extern|inline|const|unsafe|open|data|suspend|partial|readonly)\s+)*"
    r"(?:fn|func|def|fun|function|class|struct|enum|trait|impl|interface|record|module|namespace|object|type|sub|macro_rules!)\b"
)
RX_C_LIKE = re.compile(r"^\s{0,4}[A-Za-z_][\w:<>,\s*&\[\]]*\s+[*&]?([A-Za-z_][\w:~]*)\s*\([^;]*$")  # C/C++/Java/C# method heads


def _generic_outline(text: str) -> list[str]:
    out = []
    for ln, s in enumerate(text.splitlines(), 1):
        st = s.strip()
        if not st or st.startswith(("//", "#", "/*", "*", "--", "return ", "else", "if ", "if(", "for ", "while ")):
            continue
        if RX_GENERIC.match(s) or RX_C_LIKE.match(s):
            out.append(f"{ln}  {_clip(s.rstrip(' {'), 140)}")
    return out


# ---------- Import graph ----------
class Graph:
    def __init__(self, data: dict) -> None:
        self.root = data["root"]
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.out: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        self.inn: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
        for a, b, kind in data["edges"]:
            self.out[a][kind].append(b)
            self.inn[b][kind].append(a)
        self.semantic: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for doc, code, score in data["semantic"]:
            self.semantic[code].append((doc, score))
            self.semantic[doc].append((code, score))
        self.indexed = data["index"].get("built", False)

    def hops(self, start: str, direction: str, depth: int) -> list[list[str]]:
        """Import-graph layers: [[1 hop], [2 hops], ...], each file once at its shortest distance."""
        edges = self.out if direction == "out" else self.inn
        seen, layer, layers = {start}, [start], []
        for _ in range(depth):
            nxt = sorted({x for f in layer for x in edges[f]["import"]} - seen)
            if not nxt:
                break
            seen.update(nxt)
            layers.append(nxt)
            layer = nxt
        return layers

    def missing(self, rel: str) -> str:
        by_name = defaultdict(list)
        for f in self.nodes:
            by_name[posixpath.basename(f).lower()].append(f)
        names = difflib.get_close_matches(posixpath.basename(rel).lower(), list(by_name), n=4, cutoff=0.75)
        near = [f for nm in names for f in by_name[nm]]
        hint = f" Did you mean: {', '.join(near[:8])}?" if near else ""
        return f"{rel} is not a project file (wrong path, ignored by .gitignore/.faravexignore, or binary).{hint}"

    def docs(self, rel: str) -> list[str]:
        """Docs that name `rel` or are semantically linked to it."""
        return sorted(set(self.inn[rel]["ref"]) | {d for d, _ in self.semantic.get(rel, []) if depmap.kind_of(d) == "doc"})


_graphs: dict[Path, tuple[tuple, Graph]] = {}
_graphs_lock = threading.Lock()


def graph(root: Path) -> Graph:
    """Project graph, rebuilt only when a file (or the index) changed since the last call."""
    stamp = []
    for rel, abspath in iter_source_files(root):
        try:
            st = abspath.stat()
            stamp.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            pass
    db = index_dir_for(root) / "index.db"
    stamp.append(("<index>", db.stat().st_mtime_ns if db.is_file() else 0, 0))
    key = tuple(stamp)
    with _graphs_lock:
        hit = _graphs.get(root)
        if hit and hit[0] == key:
            return hit[1]
    g = Graph(depmap.build(root))
    with _graphs_lock:
        _graphs[root] = (key, g)
    return g


def _coverage_note(rel: str) -> str:
    ext = posixpath.splitext(rel.lower())[1]
    if ext in GRAPH_EXTS or depmap.kind_of(rel) != "code":
        return ""
    return (f"note: imports are parsed for Python, JS/TS, HTML and CSS only; {ext or 'this'} files have no import "
            f"edges. Use search_code or grep for usages.")


def _files(items: list[str], limit: int = 40) -> str:
    if not items:
        return "-"
    shown = [f + (" [test]" if TEST_RX.search(f) else "") for f in items[:limit]]
    return ", ".join(shown) + (f", ... +{len(items) - limit} more" if len(items) > limit else "")


def dependencies(root: Path, rel: str, depth: int = 1) -> str:
    g = graph(root)
    n = g.nodes.get(rel)
    if not n:
        return g.missing(rel)
    depth = max(1, min(depth, 10))
    out = [f"{rel} ({n['kind']}, {n['lines']} lines){' - ' + n['summary'] if n['summary'] else ''}"]
    for title, direction in (("imports", "out"), ("imported by", "in")):
        layers = g.hops(rel, direction, depth)
        if depth == 1 or not layers:
            out.append(f"{title}: {_files(layers[0] if layers else [])}")
        else:
            for k, layer in enumerate(layers, 1):
                out.append(f"{title}, {k} hop{'s' if k > 1 else ''}: {_files(layer)}")
    if n["kind"] == "doc":
        out.append(f"mentions: {_files(sorted(g.out[rel]['ref']))}")
    out.append(f"named in docs: {_files(sorted(g.inn[rel]['ref']))}")
    if g.semantic.get(rel):
        sem = sorted(g.semantic[rel], key=lambda x: -x[1])
        out.append("semantically linked: " + ", ".join(f"{f} ({s:.2f})" for f, s in sem[:8]))
    elif not g.indexed:
        out.append("semantically linked: (project not indexed; run index_codebase for doc<->code links)")
    if n["external"]:
        out.append(f"packages: {', '.join(n['external'])}")
    note = _coverage_note(rel)
    return "\n".join(out + ([note] if note else []))


def blast_radius(root: Path, rel: str, symbol: str | None = None, depth: int = 3) -> str:
    g = graph(root)
    n = g.nodes.get(rel)
    if not n:
        return g.missing(rel)
    depth = max(1, min(depth, 10))
    layers = g.hops(rel, "in", depth)
    affected = [f for layer in layers for f in layer]
    deeper = bool(layers) and len(layers) == depth and bool(g.hops(rel, "in", depth + 1)[depth:])
    out = [f"Blast radius of {rel}{f' (symbol {symbol})' if symbol else ''}: "
           f"{len(affected)} file{'s' if len(affected) != 1 else ''} import it directly or through others."]
    for k, layer in enumerate(layers, 1):
        out.append(f"{k} hop{'s' if k > 1 else ''} ({len(layer)}): {_files(layer)}")
    if deeper:
        out.append(f"(more files beyond {depth} hop{'s' if depth > 1 else ''}; raise depth to see them)")

    if symbol:
        out.append(f"lines naming {symbol}:")
        direct = set(layers[0]) if layers else set()
        silent = []
        for f in [rel, *affected]:
            hits = usage_lines(f, read_text(root / f) or "", symbol)
            if hits:
                shown = ", ".join(map(str, hits[:12])) + (f", ... +{len(hits) - 12}" if len(hits) > 12 else "")
                out.append(f"  {f}{' (itself)' if f == rel else ''}: {shown}")
            elif f in direct:
                silent.append(f)
        if silent:
            out.append(f"  direct importers that never name it (likely safe): {_files(silent)}")

    tests = [f for f in affected if TEST_RX.search(f)]
    out.append(f"tests in radius: {_files(tests)}")
    out.append(f"docs describing it: {_files(g.docs(rel))}")
    note = _coverage_note(rel)
    return "\n".join(out + ([note] if note else []))


# ---------- Context slice ----------
FENCE = {".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "jsx", ".ts": "typescript",
         ".tsx": "tsx", ".html": "html", ".css": "css", ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
         ".toml": "toml", ".go": "go", ".rs": "rust", ".java": "java", ".cs": "csharp", ".sh": "bash", ".ps1": "powershell"}


def symbol_range(rel: str, text: str, symbol: str) -> tuple[int, int] | None:
    """1-based inclusive line range of a function/class/method named `symbol` (first match)."""
    ext = posixpath.splitext(rel.lower())[1]
    if ext == ".py":
        try:
            for n in ast.walk(ast.parse(text)):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == symbol:
                    return def_start(n), n.end_lineno
        except (SyntaxError, ValueError):
            return None
    elif ext in depmap.JS_EXTS:
        rx = re.compile(rf"(?:class |interface |type |enum )?#?{re.escape(symbol)}\b")
        for _, s, e, label in js_decls(text):
            if rx.match(label):
                return s, e
    return None


def context_slice(root: Path, rel: str, symbol: str | None = None, max_chars: int = 24000) -> str:
    """One paste-ready bundle: the file (or one symbol), signatures of what it imports, how importers use it,
    and doc lines that mention it. Sections are added in that order until `max_chars` is reached."""
    g = graph(root)
    n = g.nodes.get(rel)
    if not n:
        return g.missing(rel)
    max_chars = max(2000, min(max_chars, 200_000))
    text = read_text(root / rel) or ""
    lines = text.splitlines()
    ext = posixpath.splitext(rel.lower())[1]
    lo, hi = 1, len(lines)
    if symbol:
        rng = symbol_range(rel, text, symbol)
        if rng:
            lo, hi = rng
    body = "\n".join(lines[lo - 1: hi])
    cap = int(max_chars * 0.6)
    cut = ""
    if len(body) > cap:
        body = body[:cap].rsplit("\n", 1)[0]
        cut = f"\n... (truncated; full range is lines {lo}-{hi}; use get_symbol_outline to pick a part)"
    span = f"lines {lo}-{hi} of {len(lines)}" if (lo, hi) != (1, len(lines)) else f"{len(lines)} lines"
    what = f"{symbol} in {rel}" if symbol and (lo, hi) != (1, len(lines)) else rel
    out = [f"# Context: {what}",
           f"Project: {root.name}. {n['kind']} file{' - ' + n['summary'] if n['summary'] else ''}",
           "", f"## {rel} ({span})", f"```{FENCE.get(ext, '')}", body + cut, "```"]
    if symbol and (lo, hi) == (1, len(lines)):
        out.insert(2, f"(no definition named {symbol} found; showing the whole file)")
    used = sum(map(len, out))
    skipped: list[str] = []

    def add(section: list[str], name: str) -> None:
        nonlocal used
        size = sum(len(s) + 1 for s in section)
        if used + size > max_chars:
            skipped.append(name)
            return
        out.extend(section)
        used += size

    for dep in sorted(g.out[rel]["import"]):
        try:
            add(["", f"## Imports {dep} (outline)", outline(root, dep)], dep)
        except ValueError:
            pass

    importers = sorted(g.inn[rel]["import"])
    if importers:
        name = symbol or posixpath.splitext(posixpath.basename(rel))[0]
        sec = ["", f"## Used by ({len(importers)} file{'s' if len(importers) > 1 else ''})"]
        for f in importers:
            ftext = read_text(root / f) or ""
            flines = ftext.split("\n")
            hits = [(i, flines[i - 1].strip()) for i in usage_lines(f, ftext, name)][:6]
            sec.append(f"- {f}" + ("" if hits else " (imports it)"))
            sec.extend(f"    {i}: {_clip(s, 140)}" for i, s in hits)
        add(sec, "importers")

    base = posixpath.basename(rel)
    terms = [t for t in (base, symbol) if t]
    for d in g.docs(rel):
        dtext = read_text(root / d) or ""
        hits = [(i, s.strip()) for i, s in enumerate(dtext.splitlines(), 1) if any(t in s for t in terms)][:8]
        sec = ["", f"## Doc {d}"] + ([f"    {i}: {_clip(s, 200)}" for i, s in hits] or [outline(root, d)])
        add(sec, d)

    if skipped:
        out += ["", f"(left out to stay under {max_chars} chars: {_files(skipped, 12)})"]
    note = _coverage_note(rel)
    return "\n".join(out + (["", note] if note else []))


def enclosing_scope(rel: str, text: str, start: int, end: int) -> str | None:
    """Innermost function/class containing lines start..end ("class Index > def search"), "(module level)" when
    none does, None when the language has no outline (callers fall back to chunk scopes)."""
    ext = posixpath.splitext(rel.lower())[1]
    if ext == ".py":
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return None
        trail: list[str] = []
        body = tree.body
        while True:
            inner = next((n for n in body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                          and n.lineno <= start and end <= n.end_lineno), None)
            if not inner:
                break
            trail.append(f"{'class' if isinstance(inner, ast.ClassDef) else 'def'} {inner.name}")
            body = inner.body
        return " > ".join(trail) or "(module level)"
    if ext in depmap.JS_EXTS:
        trail = []
        for depth, s, e, label in js_decls(text):
            if s <= start and end <= e:
                trail = trail[:depth] + [label]
        return " > ".join(trail) or "(top level)"
    return None
