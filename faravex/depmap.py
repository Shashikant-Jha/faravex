"""Dependency map of a project for the browser UI.

Edges, all read from the files themselves:
  import  code -> code   Python (ast), JS/TS (import / export from / require / import()), HTML src/href, CSS @import
  ref     doc  -> any    markdown links, `backticked` file names and a/b/c.ext paths in docs and specs
  semantic doc -> code   from the existing EmbeddingGemma index (no model needed): best-matching code files per doc
"""

import ast
import posixpath
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from .files import iter_source_files, read_text
from .index import index_dir_for

DOC_EXTS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
NOT_DOCS = ("requirements", "robots", "license", "licence", "notice", "copying", "constraints")  # .txt that are not prose
CONFIG_EXTS = {".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".csproj", ".env"}
JS_EXTS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".svelte", ".astro"}
JS_RESOLVE = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts", ".vue", ".svelte",
              "/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/index.mjs")
RX_JS = re.compile(r"""(?:\bimport\s+(?:[\w*{}\s,$]+?\s+from\s+)?|\bexport\s+[\w*{}\s,$]+?\s+from\s+|\brequire\s*\(\s*|\bimport\s*\(\s*)["']([^"'\n]+)["']""")
RX_HTML = re.compile(r"""\b(?:src|href)\s*=\s*["']([^"'#?\n]+)""", re.I)
RX_CSS = re.compile(r"""@import\s+(?:url\(\s*)?["']?([^"')\s;]+)""")
RX_MD_LINK = re.compile(r"\]\(\s*<?([^)\s>]+)")
RX_TICK = re.compile(r"`([^`\s]+\.[A-Za-z][A-Za-z0-9]{0,5})`")
RX_PATH = re.compile(r"(?<![\w/.:-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w/])")
SKIP_SCHEMES = ("http:", "https:", "mailto:", "data:", "javascript:", "tel:", "//", "#")

SEMANTIC_PER_DOC = 3     # strongest code files kept per doc
SEMANTIC_MIN = 0.78      # cosine floor; doc-vs-code chunk scores cluster 0.70-0.90, unrelated pairs sit near 0.70
SEMANTIC_MARGIN = 0.05   # keep only links within this of the doc's best match


def kind_of(rel: str) -> str:
    ext = posixpath.splitext(rel.lower())[1]
    if ext in DOC_EXTS and not (ext == ".txt" and posixpath.basename(rel.lower()).startswith(NOT_DOCS)):
        return "doc"
    if ext in CONFIG_EXTS or ext == ".txt":
        return "config"
    return "code"


def _summary(rel: str, text: str) -> str:
    if rel.endswith(".py"):
        try:
            doc = ast.get_docstring(ast.parse(text))
            if doc:
                return doc.strip().split("\n")[0][:160]
        except (SyntaxError, ValueError):
            pass
    for line in text.splitlines()[:40]:
        s = line.strip()
        if s.startswith("#") and kind_of(rel) == "doc":
            return s.lstrip("#").strip()[:160]
        for mark in ("//", "/*", "<!--", '"""', "#", "*"):
            if s.startswith(mark) and len(s) > len(mark) + 3 and kind_of(rel) != "doc":
                return s[len(mark):].strip(" */->\"")[:160]
    return ""


class _Resolver:
    def __init__(self, files: list[str]) -> None:
        self.files = set(files)
        self.by_name: dict[str, list[str]] = defaultdict(list)
        self.py: dict[str, list[str]] = defaultdict(list)  # dotted suffix -> files
        for rel in files:
            self.by_name[posixpath.basename(rel).lower()].append(rel)
            if rel.endswith(".py"):
                parts = rel[:-3].split("/")
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                for i in range(len(parts)):
                    self.py[".".join(parts[i:])].append(rel)

    @staticmethod
    def nearest(cands: list[str], importer: str) -> str | None:
        """Several files can answer the same module name: take the one closest to the importer."""
        if len(cands) <= 1:
            return cands[0] if cands else None
        home = importer.split("/")

        def score(c: str):
            parts, k = c.split("/"), 0
            while k < min(len(parts), len(home)) - 1 and parts[k] == home[k]:
                k += 1
            return k, -len(parts)
        return max(cands, key=score)

    def path(self, spec: str, base_dir: str, js: bool = False) -> str | None:
        if not spec or spec.startswith(SKIP_SCHEMES):
            return None
        spec = spec.split("?")[0].split("#")[0].replace("\\", "/")
        norm = posixpath.normpath(spec.lstrip("/") if spec.startswith("/") else posixpath.join(base_dir, spec))
        for ext in (JS_RESOLVE if js else ("",)):
            if norm + ext in self.files:
                return norm + ext
        return None

    def mention(self, spec: str, base_dir: str, doc: str) -> str | None:
        """A file named in prose: relative path, root path, unique path suffix, then unique file name."""
        hit = self.path(spec, base_dir) or self.path(spec, "")
        if hit:
            return hit
        spec = spec.strip("./").replace("\\", "/")
        cands = self.by_name.get(posixpath.basename(spec).lower(), [])
        if "/" in spec:
            cands = [c for c in cands if c.endswith("/" + spec) or c == spec]
        return self.nearest(cands, doc) if len(cands) == 1 else None

    def python(self, rel: str, tree: ast.AST) -> tuple[set[str], set[str]]:
        pkg = rel.split("/")[:-1]
        found, external = set(), set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    hit = self.nearest(self.py.get(a.name, []), rel)
                    found.add(hit) if hit else external.add(a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom):
                if n.level:
                    base = pkg[: len(pkg) - n.level + 1]
                    mod = "/".join(base + (n.module.split(".") if n.module else []))
                    cands = [mod + ".py", mod + "/__init__.py"] + [f"{mod}/{a.name}.py" for a in n.names]
                    hits = [c.lstrip("/") for c in cands if c.lstrip("/") in self.files]
                    found.update(h for h in hits if not h.endswith("__init__.py") or len(hits) == 1)
                elif n.module:
                    subs = [h for a in n.names if (h := self.nearest(self.py.get(f"{n.module}.{a.name}", []), rel))]
                    hit = self.nearest(self.py.get(n.module, []), rel)
                    if subs:
                        found.update(subs)
                    elif hit:
                        found.add(hit)
                    else:
                        external.add(n.module.split(".")[0])
        return found, external - set(sys.stdlib_module_names) - {"__future__"}


def build(root: Path) -> dict:
    t0 = time.perf_counter()
    root = root.resolve()
    texts: dict[str, str] = {}
    for rel, abspath in iter_source_files(root):
        text = read_text(abspath)
        if text is not None:
            texts[rel] = text
    res = _Resolver(list(texts))
    nodes, edges = [], {}
    for rel, text in texts.items():
        kind, base = kind_of(rel), posixpath.dirname(rel)
        ext = posixpath.splitext(rel.lower())[1]
        targets: set[str] = set()
        external: set[str] = set()
        if ext == ".py":
            try:
                targets, external = res.python(rel, ast.parse(text))
            except (SyntaxError, ValueError):
                pass
        elif ext in JS_EXTS:
            for spec in RX_JS.findall(text):
                if spec.startswith((".", "/")):
                    hit = res.path(spec, base, js=True)
                    if hit:
                        targets.add(hit)
                elif not spec.startswith(SKIP_SCHEMES):
                    parts = spec.split("/")
                    external.add("/".join(parts[:2]) if spec.startswith("@") else parts[0])
        elif ext in (".html", ".htm"):
            targets = {h for s in RX_HTML.findall(text) if (h := res.path(s, base))}
        elif ext in (".css", ".scss", ".sass", ".less"):
            targets = {h for s in RX_CSS.findall(text) if (h := res.path(s, base))}
        if kind == "doc":
            specs = set(RX_MD_LINK.findall(text)) | set(RX_TICK.findall(text)) | set(RX_PATH.findall(text))
            refs = {h for s in specs if (h := res.mention(s, base, rel))}
            for t in refs - {rel}:
                edges.setdefault((rel, t), "ref")
        for t in targets - {rel}:
            edges[(rel, t)] = "import"
        nodes.append({
            "id": rel,
            "kind": kind,
            "lines": len(text.splitlines()),
            "summary": _summary(rel, text),
            "external": sorted(external),
        })

    semantic, index_info = _semantic(root, texts, edges)
    return {
        "root": str(root),
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ms": round((time.perf_counter() - t0) * 1000),
        "index": index_info,
        "nodes": nodes,
        "edges": [[a, b, k] for (a, b), k in sorted(edges.items())],
        "semantic": semantic,
    }


def _semantic(root: Path, texts: dict[str, str], edges: dict) -> tuple[list, dict]:
    """doc -> code links from stored chunk embeddings. Reads the index; never builds it."""
    db = index_dir_for(root) / "index.db"
    if not db.is_file():
        return [], {"built": False}
    from . import server  # shared index objects (and their matrix cache) with search

    index = server._get_index(str(root))
    status = index.status()
    info = {"built": index.is_built, "updated_at": status["updated_at"], "chunks": status["chunks"], "stale_config": status["stale_config"]}
    matrix, ids, paths, _, _ = index._load_matrix()
    if not len(ids):
        return [], info
    paths_arr = np.array(paths)
    kinds = {p: kind_of(p) for p in set(paths)}
    doc_rows = np.array([i for i, p in enumerate(paths) if kinds[p] == "doc" and p in texts], dtype=np.int64)
    code_rows = np.array([i for i, p in enumerate(paths) if kinds[p] == "code" and p in texts], dtype=np.int64)
    if not len(doc_rows) or not len(code_rows):
        return [], info
    code_rows = code_rows[np.argsort(paths_arr[code_rows], kind="stable")]
    code_files, starts = np.unique(paths_arr[code_rows], return_index=True)
    code_m = matrix[code_rows]
    best: dict[str, np.ndarray] = {}
    for s in range(0, len(doc_rows), 512):  # blocks keep memory flat on big repos
        rows = doc_rows[s: s + 512]
        per_file = np.maximum.reduceat(matrix[rows] @ code_m.T, starts, axis=1)
        for r, i in enumerate(rows):
            p = paths[i]
            best[p] = per_file[r] if p not in best else np.maximum(best[p], per_file[r])
    links = []
    for doc, sims in best.items():
        top = float(sims.max())
        for j in np.argsort(-sims)[:SEMANTIC_PER_DOC]:
            score, code = float(sims[j]), str(code_files[j])
            if score >= max(SEMANTIC_MIN, top - SEMANTIC_MARGIN) and (doc, code) not in edges:
                links.append([doc, code, round(score, 3)])
    return links, info
