"""What uncommitted (or staged, or since-a-ref) changes touch: enclosing scopes, where changed names are used,
similar code elsewhere, and the tests likely affected. Used by the search_diff MCP tool and the map's Changes mode."""

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import navigate
from .files import read_text

MAX_HUNKS = 16
MERGE_GAP = 15          # hunks of one file closer than this many lines are treated as one change
MAX_HUNK_CHARS = 3000   # embedded text per change
RX_DEFINES = [
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*class\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="),
    re.compile(r"^([A-Z][A-Z0-9_]{2,})\s*[:=]"),  # module constants
]
RX_IMPORT = re.compile(r"^\s*(import\s|from\s+\S+\s+import\s|(?:export\s+)?(?:const|let|var)\s+.*=\s*require\(|#include\s|using\s)")
SKIP_NAMES = {"__init__", "main", "self", "this", "constructor", "init", "setup", "run", "get", "set"}


@dataclass
class Change:
    path: str
    start: int  # new-file lines; 0 when the file was deleted
    end: int
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    status: str = "modified"  # modified | added | deleted | untracked
    scope: str = ""
    names: list[str] = field(default_factory=list)
    usages: list[tuple[str, list[int]]] = field(default_factory=list)
    similar: list[dict] = field(default_factory=list)


def _git(root: Path, *args: str) -> str:
    try:
        # stdin=DEVNULL: inside the MCP server stdin is the protocol pipe; a child inheriting it can hang.
        r = subprocess.run(["git", "-C", str(root), *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise ValueError("git is not installed or not on PATH") from None
    if r.returncode != 0:
        raise ValueError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:300]}")
    return r.stdout


def read_changes(root: Path, staged: bool = False, base: str | None = None) -> tuple[list[Change], str]:
    """Hunks from git diff (zero context), nearby hunks of a file merged. Untracked files count as added
    unless staged or base is given."""
    _git(root, "rev-parse", "--show-toplevel")  # fails clearly outside a repo
    args = ["diff", "--unified=0", "--no-color", "--no-ext-diff", "--relative"]
    if base:
        if base.startswith("-"):
            raise ValueError("base must be a git ref, e.g. main or HEAD~3")
        args.append(base)
        what = f"working tree vs {base}"
    elif staged:
        args.append("--cached")
        what = "staged changes"
    else:
        what = "uncommitted changes (working tree vs index, plus untracked files)"
    changes: list[Change] = []
    path, status, cur = None, "modified", None
    for line in _git(root, *args).splitlines():
        if line.startswith("diff --git"):
            path, status, cur = None, "modified", None
        elif line.startswith("new file mode"):
            status = "added"
        elif line.startswith("deleted file mode"):
            status = "deleted"
        elif line.startswith("--- a/") and status == "deleted":
            path = line[6:]
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@") and path:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            s, n = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            start, end = (0, 0) if status == "deleted" else (max(s, 1), max(s, 1) + max(n, 1) - 1)
            prev = changes[-1] if changes else None
            if prev and prev.path == path and status != "deleted" and start - prev.end <= MERGE_GAP:
                prev.end = max(prev.end, end)
                cur = prev
            else:
                cur = Change(path, start, end, status=status)
                changes.append(cur)
        elif cur is not None and line[:1] in "+-" and not line.startswith(("+++", "---")):
            (cur.added if line[0] == "+" else cur.removed).append(line[1:])
    if not staged and not base:
        for rel in _git(root, "ls-files", "--others", "--exclude-standard").splitlines():
            text = read_text(root / rel)
            if text is not None:
                lines = text.splitlines()
                changes.append(Change(rel, 1, max(1, len(lines)), added=lines, status="untracked"))
    return changes, what


def _names(ch: Change) -> list[str]:
    found = []
    for line in ch.added + ch.removed:
        for rx in RX_DEFINES:
            if m := rx.match(line):
                found.append(m.group(1))
                break
    for part in re.findall(r"(?:def|class) ([A-Za-z_$][\w$]*)|([A-Za-z_$][\w$]*)\(", ch.scope.rsplit(" > ", 1)[-1]):
        found.append(part[0] or part[1])
    return [n for n in dict.fromkeys(found) if len(n) >= 3 and n not in SKIP_NAMES][:6]


def analyse(root: Path, index, staged: bool = False, base: str | None = None, top_k: int = 5) -> dict:
    """Structured result for the UI; format_text() renders it for agents."""
    changes, what = read_changes(root, staged, base)
    total = len(changes)
    changes = [c for c in changes if c.status != "deleted" or c.removed][:MAX_HUNKS]
    if not changes:
        return {"what": what, "changes": [], "tests": [], "total": 0}
    index.update()  # chunks (and scopes) reflect the working tree
    matrix, ids, paths, starts, ends = index._load_matrix()
    scopes = dict(index.db.execute("SELECT id, scope FROM chunks").fetchall())
    g = navigate.graph(root)

    texts = []
    for ch in changes:
        if ch.status != "deleted":
            text = read_text(root / ch.path) or ""
            exact = navigate.enclosing_scope(ch.path, text, ch.start, ch.end)
            ch.scope = exact if exact is not None else ", ".join(dict.fromkeys(  # no outline: the chunks' scopes
                scopes.get(int(ids[i]), "") for i in range(len(ids))
                if paths[i] == ch.path and starts[i] <= ch.end and ch.start <= ends[i] and scopes.get(int(ids[i]))))[:200]
        ch.names = _names(ch)
        body = "\n".join(ch.added if ch.added else ch.removed)
        texts.append((f"{ch.path} > {ch.scope}" if ch.scope else ch.path, body[:MAX_HUNK_CHARS]))

    # Similar code: the change embedded as a document (code <-> code), compared with every other file's chunks.
    vecs = index.embedder.embed_documents(texts) if len(ids) else np.zeros((0, 0))
    for ch, vec in zip(changes, vecs):
        scores = matrix @ vec
        picked, seen_files = [], set()
        changed = [x for x in (ch.added or ch.removed) if x.strip()]
        if sum(len(x.strip()) for x in changed) < 40 or all(RX_IMPORT.match(x) for x in changed):
            scores = scores[:0]  # an import line or a tiny edit: "similar code" would only be noise
        for i in np.argsort(-scores):
            if paths[i] == ch.path or paths[i] in seen_files:
                continue
            seen_files.add(paths[i])
            picked.append({"path": paths[i], "start": int(starts[i]), "end": int(ends[i]), "score": round(float(scores[i]), 3),
                           "scope": scopes.get(int(ids[i]), ""), "test": bool(navigate.TEST_RX.search(paths[i]))})
            if len(picked) >= top_k:
                break
        ch.similar = picked

        # Usages: files the keyword index finds the name in, checked line by line for uses in code.
        hits: dict[str, set[int]] = {}
        for name in ch.names:
            for p in dict.fromkeys(h.path for h in index.search(name, top_k=20, mode="keyword") if h.path != ch.path):
                if lines := navigate.usage_lines(p, read_text(root / p) or "", name):
                    hits.setdefault(p, set()).update(lines)
        ch.usages = sorted(((p, sorted(ls)) for p, ls in hits.items()), key=lambda x: (not navigate.TEST_RX.search(x[0]), x[0]))[:10]

    tests = set()
    for ch in changes:
        if navigate.TEST_RX.search(ch.path):
            tests.add(ch.path)
        if ch.path in g.nodes:
            for layer in g.hops(ch.path, "in", 3):
                tests.update(f for f in layer if navigate.TEST_RX.search(f))
        tests.update(p for p, _ in ch.usages if navigate.TEST_RX.search(p))
        tests.update(s["path"] for s in ch.similar if s["test"])
    return {
        "what": what, "total": total, "tests": sorted(tests),
        "changes": [{"path": c.path, "start": c.start, "end": c.end, "status": c.status, "added": len(c.added),
                     "removed": len(c.removed), "scope": c.scope, "names": c.names,
                     "usages": [{"path": p, "lines": ls} for p, ls in c.usages], "similar": c.similar} for c in changes],
    }


def format_text(r: dict) -> str:
    if not r["changes"]:
        return f"No changes ({r['what']})."
    files = len({c["path"] for c in r["changes"]})
    more = f" (showing first {len(r['changes'])} of {r['total']})" if r["total"] > len(r["changes"]) else ""
    out = [f"{len(r['changes'])} changes in {files} files: {r['what']}{more}"]
    for c in r["changes"]:
        where = f"{c['path']}:{c['start']}-{c['end']}" if c["status"] != "deleted" else f"{c['path']} (deleted)"
        tag = f" [{c['status']}]" if c["status"] != "modified" else ""
        out += ["", f"## {where} (+{c['added']} -{c['removed']}){tag}" + (f"  in {c['scope']}" if c["scope"] else "")]
        if c["names"]:
            out.append(f"names touched: {', '.join(c['names'])}")
        if c["usages"]:
            out.append("used in: " + "; ".join(
                f"{u['path']}:{','.join(map(str, u['lines'][:8]))}" + (" [test]" if navigate.TEST_RX.search(u["path"]) else "")
                for u in c["usages"]))
        if c["similar"]:
            out.append("similar code elsewhere:")
            out += [f"  {s['path']}:{s['start']}-{s['end']} ({s['score']:.2f}){' [test]' if s['test'] else ''}"
                    f"{'  ' + s['scope'] if s['scope'] else ''}" for s in c["similar"]]
    out += ["", f"tests likely affected: {', '.join(r['tests']) if r['tests'] else '- (none found by imports, usages or similarity)'}"]
    return "\n".join(out)
