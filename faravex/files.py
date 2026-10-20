"""Source file discovery (respects .gitignore / .faravexignore) and chunking."""

import ast
import os
import re
from pathlib import Path
from typing import Iterator

import pathspec

from . import config

CODE_EXTS = {
    ".py", ".pyi", ".ipynb", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".vue", ".svelte", ".astro", ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".cs", ".fs", ".vb",
    ".go", ".rs", ".swift", ".m", ".mm", ".rb", ".php", ".pl", ".pm", ".lua", ".r",
    ".dart", ".ex", ".exs", ".erl", ".hs", ".clj", ".elm", ".jl", ".nim", ".zig", ".sol",
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd",
    ".sql", ".graphql", ".gql", ".proto", ".thrift",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".csproj",
    ".tf", ".hcl", ".nix", ".cmake", ".md", ".mdx", ".rst", ".txt",
}
CODE_NAMES = {"dockerfile", "makefile", "rakefile", "gemfile", "jenkinsfile", "procfile", "cmakelists.txt"}
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", "target", "out", "bin", "obj",
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".cache", ".gradle", ".idea", ".vs", "coverage",
    "vendor", "Pods", ".terraform", "site-packages", "indexes", "models",
}
SKIP_SUFFIXES = (".min.js", ".min.css", ".map", ".lock", "-lock.json", ".lock.json")
IGNORE_FILES = (".gitignore", ".faravexignore")


def _load_spec(directory: Path) -> pathspec.PathSpec | None:
    lines: list[str] = []
    for name in IGNORE_FILES:
        f = directory / name
        if f.is_file():
            try:
                lines += f.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                pass
    return pathspec.GitIgnoreSpec.from_lines(lines) if lines else None


def iter_source_files(root: Path) -> Iterator[tuple[str, Path]]:
    """Yield (posix relative path, absolute path) for indexable files under root."""
    root = root.resolve()
    specs: list[tuple[str, pathspec.PathSpec]] = []  # (relative dir prefix, spec)

    def ignored(rel: str, is_dir: bool) -> bool:
        for prefix, spec in specs:
            if rel.startswith(prefix) and spec.match_file(rel[len(prefix):] + ("/" if is_dir else "")):
                return True
        return False

    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        rel_dir = d.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir + "/"
        specs[:] = [(p, s) for p, s in specs if rel_dir.startswith(p)]
        spec = _load_spec(d)
        if spec:
            specs.append((rel_dir, spec))

        dirnames[:] = sorted(
            n for n in dirnames
            if n not in SKIP_DIRS and (n == ".github" or not n.startswith(".")) and not ignored(rel_dir + n, True)
        )
        for name in sorted(filenames):
            lower = name.lower()
            ext = os.path.splitext(lower)[1]
            if lower.endswith(SKIP_SUFFIXES):
                continue
            if ext not in CODE_EXTS and ext not in config.EXTRA_EXTS and lower not in CODE_NAMES:
                continue
            rel = rel_dir + name
            if ignored(rel, False):
                continue
            yield rel, d / name


def read_text(path: Path) -> str | None:
    """Return file text, or None if too big or binary."""
    try:
        if path.stat().st_size > config.MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def _cut_score(lines: list[str], i: int) -> int:
    """How good a place it is to start a new chunk at line i."""
    line = lines[i]
    if line.strip() and not line[0].isspace() and line.lstrip()[0] not in ")]}":
        return 2  # top-level construct starts here
    if not lines[i - 1].strip():
        return 1  # after a blank line
    return 0


def chunk_text(text: str) -> list[tuple[int, int, str]]:
    """Split text into (start_line, end_line, chunk) with 1-based inclusive lines.

    Chunks prefer to end right before a top-level definition or a blank line so that
    functions/classes stay together; otherwise they overlap by CHUNK_OVERLAP lines.
    """
    lines = text.splitlines()
    n = len(lines)
    max_lines, max_chars = config.CHUNK_MAX_LINES, config.CHUNK_MAX_CHARS
    min_lines = max_lines // 4
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < n:
        end, chars = start, 0
        while end < n and end - start < max_lines:
            if end > start and chars + len(lines[end]) > max_chars:
                break
            chars += len(lines[end]) + 1
            end += 1

        score = 2
        if end < n:
            best, score = end, 0
            for i in range(end, min(start + min_lines, end - 1), -1):
                s = _cut_score(lines, i)
                if s > score:
                    best, score = i, s
                    if s == 2:
                        break
            end = best

        body = "\n".join(lines[start:end])
        if body.strip():
            chunks.append((start + 1, end, body[: max_chars * 2]))
        if end >= n:
            break
        overlap = min(config.CHUNK_OVERLAP, (end - start) // 4)
        start = end if score == 2 else max(end - overlap, start + 1)
    return chunks


# ---------- Syntax-aware chunking ----------
# Chunks follow functions, classes, methods and markdown sections, and each carries its scope ("class Index > def
# search"), which is embedded as the chunk's title so even a short body is grounded in where it lives.
# Bump CHUNKER when the output changes: it is part of the index signature, so indexes rebuild themselves.
CHUNKER = "syntax-1"
RX_COMMENT = re.compile(r"^\s*(#|//|/\*|\*|<!--)")


def def_start(n) -> int:
    """First line of a definition, including its decorators."""
    return min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])


def md_headings(lines: list[str]) -> Iterator[tuple[int, int, str]]:
    """(line, level, title) of markdown headings outside code fences."""
    fence = False
    for i, s in enumerate(lines, 1):
        if s.lstrip().startswith(("```", "~~~")):
            fence = not fence
        elif not fence and (m := re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", s)):
            yield i, len(m.group(1)), m.group(2)


def _anchors_py(text: str, lines: list[str]) -> list[tuple[int, str]] | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    out: list[tuple[int, str]] = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((def_start(n), f"def {n.name}"))
        elif isinstance(n, ast.ClassDef):
            out.append((def_start(n), f"class {n.name}"))
            if not _fits(lines, def_start(n), n.end_lineno):  # too big for one chunk: one unit per method
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        kind = "class" if isinstance(m, ast.ClassDef) else "def"
                        out.append((def_start(m), f"class {n.name} > {kind} {m.name}"))
        elif not out or out[-1][1] != "(module level)":
            out.append((n.lineno, "(module level)"))
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((n.end_lineno + 1, "(module level)"))  # whatever follows, until the next definition
    return out


def _anchors_js(text: str, lines: list[str]) -> list[tuple[int, str]]:
    from .navigate import js_decls  # lazy: navigate imports this module

    out: list[tuple[int, str]] = []
    cls = None  # name of the big class whose members are their own units
    for depth, s, e, label in js_decls(text):
        if depth == 0:
            big = label.startswith("class ") and not _fits(lines, s, e)  # small class: one unit
            cls = label.split("<")[0].split(" extends")[0] if big else None
            out.append((s, label))
            out.append((e + 1, "(top level)"))  # whatever follows, until the next declaration
        elif cls and depth == 1:
            out.append((s, f"{cls} > {label}"))
            out.append((e + 1, cls))
    return out


def _anchors_md(lines: list[str]) -> list[tuple[int, str]]:
    out, trail = [], []
    for i, level, title in md_headings(lines):
        if level <= 3:
            trail = trail[: level - 1] + [title[:60]]
            out.append((i, " > ".join(trail[1:] if len(trail) > 1 else trail)))  # the H1 is the document title
    return out


def _fits(lines: list[str], s: int, e: int) -> bool:
    return e - s + 1 <= config.CHUNK_MAX_LINES and sum(len(x) + 1 for x in lines[s - 1: e]) <= config.CHUNK_MAX_CHARS


def chunk_file(rel: str, text: str) -> list[tuple[int, int, str, str]]:
    """(start_line, end_line, chunk, scope) on syntax boundaries; falls back to chunk_text (scope "") when the
    language has no outline. Small neighbouring units are merged up to half the chunk limits."""
    lines = text.splitlines()
    n = len(lines)
    ext = Path(rel).suffix.lower()
    anchors = None
    if ext == ".py":
        anchors = _anchors_py(text, lines)
    elif ext in {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}:
        anchors = _anchors_js(text, lines)
    elif ext in {".md", ".mdx"}:
        anchors = _anchors_md(lines)
    if not anchors:
        return [(s, e, body, "") for s, e, body in chunk_text(text)]

    # Anchors -> contiguous units covering every line. Comments / decorators directly above a definition join it.
    anchors = sorted({s: label for s, label in anchors if 1 <= s <= n}.items())
    if anchors[0][0] > 1:
        anchors.insert(0, (1, "(top level)" if ext != ".py" else "(module level)"))
    starts = [s for s, _ in anchors]
    for k in range(1, len(starts)):
        while starts[k] - 1 > starts[k - 1] and RX_COMMENT.match(lines[starts[k] - 2]):
            starts[k] -= 1
    units = [(starts[k], (starts[k + 1] - 1) if k + 1 < len(starts) else n, anchors[k][1]) for k in range(len(starts))]
    units = [(s, e, label) for s, e, label in units if any(lines[i].strip() for i in range(s - 1, e))]

    out: list[tuple[int, int, str, str]] = []
    buf: list[tuple[int, int, str]] = []
    half_lines, half_chars = config.CHUNK_MAX_LINES // 2, config.CHUNK_MAX_CHARS // 2

    def flush() -> None:
        if not buf:
            return
        s, e = buf[0][0], buf[-1][1]
        labels = list(dict.fromkeys(label for _, _, label in buf))
        heads = {lb.rsplit(" > ", 1)[0] for lb in labels}
        if len(labels) > 1 and len(heads) == 1 and " > " in labels[0]:  # same class: "class X > def a, def b"
            names = [lb.rsplit(" > ", 1)[1] for lb in labels]
            scope = f"{heads.pop()} > " + ", ".join(names[:4]) + (", ..." if len(names) > 4 else "")
        else:
            scope = ", ".join(labels[:4]) + (", ..." if len(labels) > 4 else "")
        out.append((s, e, "\n".join(lines[s - 1: e]), scope))
        buf.clear()

    for s, e, label in units:
        if not _fits(lines, s, e):  # long function / section: line chunks inside it, all with its scope
            flush()
            for cs, ce, body in chunk_text("\n".join(lines[s - 1: e])):
                out.append((s + cs - 1, s + ce - 1, body, label))
            continue
        if buf and (e - buf[0][0] + 1 > half_lines or sum(len(x) + 1 for x in lines[buf[0][0] - 1: e]) > half_chars):
            flush()
        buf.append((s, e, label))
    flush()
    return out
