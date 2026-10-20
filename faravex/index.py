"""Per-project incremental index in SQLite: chunk vectors (brute-force cosine) plus an FTS5 trigram keyword index,
merged with reciprocal rank fusion so exact identifiers and natural-language questions both work."""

import fnmatch
import hashlib
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config
from .embedder import Embedder
from .files import chunk_file, iter_source_files, read_text

log = logging.getLogger("faravex")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, sha1 TEXT);
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY, path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
    text TEXT NOT NULL, emb BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
"""
# Keyword side. trigram = case-insensitive substring match, so HTTP_401_UNAUTHORIZED, "401_UNAUTH", a regex fragment or
# a UUID all hit without identifier-splitting rules. External content: the text is stored once, in `chunks`.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, path, content='chunks', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS chunks_fts_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, path) VALUES (new.id, new.text, new.path); END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, path) VALUES ('delete', old.id, old.text, old.path); END;
"""
FTS_VERSION = "trigram-1"
FLUSH_CHUNKS = 256  # chunks embedded + committed per batch
RRF_K = 60          # reciprocal rank fusion constant (the usual value)
CANDIDATES = 200    # per ranking, before fusion
KW_BONUS = 0.02     # plain questions: most a keyword match can add to a cosine score
# Words with no keyword signal in a question about code; the vector side still sees the whole query.
STOP = set("""a an and are as at be by can do does for from has have how i in is it its of on or that the this to was
were what when where which who why will with into done code file files function functions method methods class
implemented implementation handle handled handles handling logic used using use find show get set all any some""".split())
RX_TERM = re.compile(r"[\w$@#.:/\\-]{3,}")
RX_IDENT = re.compile(r"[_\d$]|[a-z][A-Z]|^[A-Z]{2,}|\w[.:/\\]\w|\w-\w")  # snake_case, digits, camelCase, CAPS, a.b, a/b


def keyword_query(query: str) -> tuple[str | None, bool]:
    """FTS5 MATCH expression for a free-text query, and whether it names something exact (identifier, quoted text)."""
    quoted = [q for q in re.findall(r'"([^"]+)"', query) if len(q) >= 3]
    rest = re.sub(r'"[^"]*"', " ", query)
    terms = [t.strip(".:/\\-") for t in RX_TERM.findall(rest)]
    terms = [t for t in terms if len(t) >= 3 and t.lower() not in STOP]
    # Names: quoted text, identifier-shaped terms, or a lone word (someone typing one word is looking for a name).
    exact = bool(quoted) or any(RX_IDENT.search(t) for t in terms) or (len(terms) == 1 and len(rest.split()) == 1)
    phrases = list(dict.fromkeys(quoted + terms))[:16]
    if not phrases:
        return None, False
    return " OR ".join('"' + ph.replace('"', '""') + '"' for ph in phrases), exact


@dataclass
class Hit:
    path: str
    start_line: int
    end_line: int
    score: float
    text: str
    match: str = "semantic"  # semantic | keyword | both
    scope: str = ""          # e.g. "class CodeIndex > def search"; empty for line-chunked files


def index_dir_for(root: Path) -> Path:
    key = str(root.resolve()).lower().replace("\\", "/")
    return config.INDEX_DIR / f"{root.resolve().name}-{hashlib.sha1(key.encode()).hexdigest()[:10]}"


class CodeIndex:
    def __init__(self, root: Path, embedder: Embedder) -> None:
        self.root = root.resolve()
        self.embedder = embedder
        self.dir = index_dir_for(self.root)
        self.dir.mkdir(parents=True, exist_ok=True)
        # WAL + busy timeout: the MCP server and the browser UI may open the same index at once.
        self.db = sqlite3.connect(self.dir / "index.db", check_same_thread=False, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        if "scope" not in {r[1] for r in self.db.execute("PRAGMA table_info(chunks)")}:
            self.db.execute("ALTER TABLE chunks ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
        self.fts = self._init_fts()
        self.db.execute("INSERT OR REPLACE INTO meta VALUES('root', ?)", (str(self.root),))
        self.db.commit()
        self.lock = threading.RLock()
        self._cache: tuple[np.ndarray, np.ndarray, list[str], np.ndarray, np.ndarray] | None = None
        self._cache_version = -1

    def _init_fts(self) -> bool:
        """Create the keyword index; fill it once from stored chunk text (no re-embedding) for older indexes."""
        try:
            self.db.executescript(FTS_SCHEMA)
            if self._meta("fts") != FTS_VERSION:
                self.db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
                self._set_meta("fts", FTS_VERSION)
                self.db.commit()
            return True
        except sqlite3.OperationalError as e:  # Python built without FTS5: vector search only
            log.warning("keyword index unavailable (%s); semantic search only", e)
            return False

    def _keyword_rows(self, match: str) -> list[int]:
        """Chunk ids by BM25 (text weighs more than path)."""
        rows = self.db.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts, 1.0, 0.3) LIMIT ?",
            (match, CANDIDATES)).fetchall()
        return [r[0] for r in rows]

    def _meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?, ?)", (key, value))

    @property
    def is_built(self) -> bool:
        return self._meta("updated_at") is not None

    def update(self, full: bool = False) -> dict:
        """Incrementally (re)index changed files. Returns stats."""
        with self.lock:
            t0 = time.time()
            sig = self.embedder.signature
            if full or self._meta("signature") != sig:
                self.db.execute("DELETE FROM chunks")
                self.db.execute("DELETE FROM files")
                self._set_meta("signature", sig)
                self.db.commit()

            known = {p: (m, s, h) for p, m, s, h in self.db.execute("SELECT path, mtime_ns, size, sha1 FROM files")}
            seen: set[str] = set()
            pending: list[tuple[str, int, int, str, list[tuple[int, int, str]]]] = []
            pending_chunks = 0
            stats = {"files_scanned": 0, "files_embedded": 0, "chunks_embedded": 0, "files_removed": 0}

            for rel, abspath in iter_source_files(self.root):
                try:
                    st = abspath.stat()
                except OSError:
                    continue
                seen.add(rel)
                stats["files_scanned"] += 1
                old = known.get(rel)
                if old and old[0] == st.st_mtime_ns and old[1] == st.st_size:
                    continue
                text = read_text(abspath)
                if text is None:
                    seen.discard(rel)
                    continue
                sha = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()
                if old and old[2] == sha:
                    self.db.execute("UPDATE files SET mtime_ns=?, size=? WHERE path=?", (st.st_mtime_ns, st.st_size, rel))
                    continue
                chunks = chunk_file(rel, text)
                pending.append((rel, st.st_mtime_ns, st.st_size, sha, chunks))
                pending_chunks += len(chunks)
                if pending_chunks >= FLUSH_CHUNKS:
                    self._flush(pending, stats)
                    pending, pending_chunks = [], 0
            self._flush(pending, stats)

            removed = [p for p in known if p not in seen]
            for p in removed:
                self.db.execute("DELETE FROM chunks WHERE path=?", (p,))
                self.db.execute("DELETE FROM files WHERE path=?", (p,))
            stats["files_removed"] = len(removed)
            self._set_meta("updated_at", time.strftime("%Y-%m-%d %H:%M:%S"))
            self.db.commit()
            if stats["files_embedded"] or removed:
                self._cache = None
            stats["seconds"] = round(time.time() - t0, 2)
            return stats

    def _flush(self, pending, stats) -> None:
        if not pending:
            return
        # The scope is embedded as the title: "path > class X > def m" grounds short bodies in where they live.
        items = [(f"{rel} > {scope}" if scope else rel, body) for rel, _, _, _, chunks in pending for _, _, body, scope in chunks]
        emb = self.embedder.embed_documents(items) if items else np.zeros((0, 0), np.float32)
        k = 0
        for rel, mtime, size, sha, chunks in pending:
            self.db.execute("DELETE FROM chunks WHERE path=?", (rel,))
            self.db.executemany(
                "INSERT INTO chunks(path, start_line, end_line, text, emb, scope) VALUES(?,?,?,?,?,?)",
                [(rel, s, e, body, emb[k + i].tobytes(), scope) for i, (s, e, body, scope) in enumerate(chunks)],
            )
            k += len(chunks)
            self.db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?,?)", (rel, mtime, size, sha))
        self.db.commit()
        stats["files_embedded"] += len(pending)
        stats["chunks_embedded"] += len(items)
        log.info("indexed %d chunks so far (%s)", stats["chunks_embedded"], pending[-1][0])

    def _load_matrix(self):
        # data_version changes when another process (MCP server or UI) commits to this index.
        version = self.db.execute("PRAGMA data_version").fetchone()[0]
        if version != self._cache_version:
            self._cache, self._cache_version = None, version
        if self._cache is None:
            rows = self.db.execute("SELECT id, path, start_line, end_line, emb FROM chunks").fetchall()
            if rows:
                matrix = np.vstack([np.frombuffer(r[4], dtype=np.float32) for r in rows])
            else:
                matrix = np.zeros((0, 1), np.float32)
            self._cache = (
                matrix,
                np.array([r[0] for r in rows], dtype=np.int64),
                [r[1] for r in rows],
                np.array([r[2] for r in rows], dtype=np.int64),
                np.array([r[3] for r in rows], dtype=np.int64),
            )
        return self._cache

    def search(self, query: str, top_k: int = 8, glob: str | None = None, mode: str = "hybrid") -> list[Hit]:
        """mode: hybrid (vectors + keywords, fused), semantic (vectors only), keyword (FTS only; no model needed)."""
        with self.lock:
            matrix, ids, paths, starts, ends = self._load_matrix()
            if not len(ids):
                return []
            allowed = np.ones(len(ids), bool)
            if glob:
                pats = [g.strip() for g in glob.split(",") if g.strip()]
                allowed = np.array([any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch(p.rsplit("/", 1)[-1], g) for g in pats) for p in paths])

            keyword: list[int] = []  # row positions (into ids), best first
            match, exact = keyword_query(query) if self.fts and mode != "semantic" else (None, False)
            if match:
                try:
                    pos = {int(r): i for i, r in enumerate(ids)}
                    keyword = [pos[r] for r in self._keyword_rows(match) if r in pos and allowed[pos[r]]]
                except sqlite3.OperationalError as e:
                    log.warning("keyword query failed (%s): %s", match, e)

            dense: list[int] = []
            if mode == "keyword":
                scores = np.zeros(len(ids), np.float32)
                order = keyword
            else:
                scores = np.where(allowed, matrix @ self.embedder.embed_query(query), -np.inf)
                dense = [int(i) for i in np.argsort(-scores)[:CANDIDATES] if np.isfinite(scores[i])]
                fused: dict[int, float] = {}
                if exact:
                    # Names in the query: rank fusion with the keyword ranking weighted up, so the chunks that
                    # contain the name come first even when the vectors rank them low.
                    for r, i in enumerate(dense):
                        fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + r)
                    for r, i in enumerate(keyword):
                        fused[i] = fused.get(i, 0.0) + 3.0 / (RRF_K + r)
                else:
                    # Plain questions: similarity decides; keyword hits only win near-ties (rank fusion would
                    # ignore that the top hit is far ahead and let common words reorder good results).
                    for i in dense:
                        fused[i] = float(scores[i])
                    for r, i in enumerate(keyword):
                        fused[i] = float(scores[i]) + KW_BONUS * (1 - r / CANDIDATES)
                order = sorted(fused, key=lambda i: -fused[i])
            # labels only: "both" = a keyword hit that the vectors also rank near the top
            # (plain questions: common words match nearly everywhere, so no label)
            in_kw = set(keyword) if exact or mode == "keyword" else set()
            in_dense = set(dense[:max(20, 3 * top_k)])

            picked: list[int] = []
            for i in order:
                if len(picked) >= top_k:
                    break
                # skip chunks overlapping an already-picked chunk of the same file
                if any(paths[j] == paths[i] and starts[i] <= ends[j] and starts[j] <= ends[i] for j in picked):
                    continue
                picked.append(int(i))

            texts = {r[0]: (r[1], r[2]) for r in self.db.execute(
                f"SELECT id, text, scope FROM chunks WHERE id IN ({','.join('?' * len(picked))})",
                [int(ids[i]) for i in picked],
            )} if picked else {}
            def how(i: int) -> str:
                return "both" if i in in_kw and i in in_dense else "keyword" if i in in_kw else "semantic"
            return [Hit(paths[i], int(starts[i]), int(ends[i]), float(scores[i]), texts[int(ids[i])][0], how(i),
                        texts[int(ids[i])][1]) for i in picked]

    def status(self) -> dict:
        with self.lock:
            files = self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunks = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            return {
                "root": str(self.root),
                "index_dir": str(self.dir),
                "files": files,
                "chunks": chunks,
                "updated_at": self._meta("updated_at"),
                "stale_config": self._meta("signature") not in (None, self.embedder.signature),
            }
