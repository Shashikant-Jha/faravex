# Faravex — offline semantic search, blast-radius graph, and live execution tracer (MCP + Browser UI)

**Faravex** gives AI coding agents (Claude Code, Copilot, Cline) and developers three synchronized views of a codebase from one local SQLite index:
- **Structure (`A` — AST):** Symbol outlines, multi-hop import/importer graphs, markdown doc links, git-diff impact analysis, and token-budgeted context slices.
- **Meaning & Text (`V` — Vector + Trigram):** Hybrid search combining local **EmbeddingGemma2** embeddings with an **FTS5 trigram** index for exact identifiers, error codes, and natural-language queries (8–10 ms median warm on GPU across 2.6k–32k-line codebases; exact text alone ~1 ms).
- **Runtime (`Ex` — Execution):** Live per-request latency, self-time flame bars, and baseline comparisons for Python 3.12+ apps (via an idle-free `.pth` hook) and JS/TS frontends (via Chrome DevTools V8 profiler + coverage).
- **Air-Gap (`Fara` — Faraday cage):** All non-loopback TCP/UDP sockets and DNS are blocked inside the process; UI and MCP bind strictly to `127.0.0.1`. Nothing ever leaves the machine.

Every feature and how to use it: [FEATURES.md](FEATURES.md).

Modules: **Search**, **Map**, **Changes**, **Record**, **Compare**, **Calls**.

Two front doors, one engine and one index:
- **MCP (HTTP or stdio)** for any MCP client (Claude Code, GitHub Copilot, Cline, ...):
  - `search_code`: by meaning and by exact text (identifiers, error codes, quoted strings), ranked line ranges;
  - `get_symbol_outline`: classes, functions and signatures of a file with line ranges (Python via `ast`;
    JS/TS via a brace-aware scanner; markdown headings; JSON/YAML/TOML keys; other languages heuristically);
  - `get_dependencies`: imports, importers (any depth), docs naming the file, semantically linked docs;
  - `get_blast_radius`: files and tests a change can break, by import hop; with `symbol`, the exact lines using it;
  - `get_context_slice`: one paste-ready bundle: the file (or one symbol), outlines of what it imports, the lines
    in importers that use it, and doc lines that mention it, cut to a character budget;
  - `search_diff`: what uncommitted (or staged, or since-a-ref) git changes touch: the enclosing function/class,
    lines elsewhere that use the changed names, similar code in other files, and the tests likely affected;
  - `trace`: live execution trace and latency of a running Python 3.12+ app picked by port (`enable` once per
    project, then `start` / `stop`), or of the JS/TS a web page runs (`start` with `url`; see Browser recording below):
    one run per request, grouped by entry point (`POST /api/orders`) with median /
    p95 / max time, the functions and files where the time goes (self time), exceptions, calls across files,
    labelled with the app, port and command it came from;
    `show` with `run=<id>` gives one run's call timeline with its inputs and output;
  - `get_execution_slice`: the source of only the functions a trace (or one run of it) executed;
  - `index_codebase`, `index_status`.

  Import graph scope (by decision, 2026-10-20): Python and JS/TS (plus HTML/CSS links). Other languages are
  searchable, but `get_dependencies`, `get_blast_radius` and `get_context_slice` find no importers for them and
  say so in a `note:` line; use `search_code` for their usages.

  Tools take `file` relative to `path` (project root), or an absolute `file` alone: the root is then the
  enclosing git repo, since other clients may start the server outside the project.
- **Browser UI** (`start.bat`): pick a folder, see its dependency map read from code, docs and specs.
  Drag nodes to untangle dense areas (positions are kept per project; **Reset layout** forgets them).
  **Search** (meaning or exact names) lights up the matching files, numbers them by rank and highlights the import paths
  between them (are the hits one subsystem or several?); a result opens the file at the matched lines.
  **Copy context** puts the selected file's context slice on the clipboard for chats without MCP.
  **Changes** shows your uncommitted edits (Δ) with the files that use them, similar code and tests to run.
  The left rail switches between the **Map** and **Record**. Record (with the port of a running Python 3.12+ app,
  or a page URL)
  shows every request as a card, grouped by endpoint with median / p95 / max and where the time goes; each card has
  the inputs (entry arguments and request body, trimmed, with password/token/secret/api_key/authorization/cookie
  values masked), the result or exception, and a call timeline whose bars show when each call ran and how long (calls
  under 0.5% of the run fold away). Each row shows how long the call took, call to return. The bright part of its bar
  is time inside that function itself (its own code plus library calls and network waits: **self** time); the dim part
  is waiting on the project calls listed under it. The yellow number marks the call with the most
  self time in that run (the bottleneck). Hover a row for both numbers. Rows overlap (a parent contains its
  children, parallel tasks run side by side), so they don't add up to the run's time. An endpoint whose bar is dim
  while a call below it (say `fetch_rates()`) is bright is waiting on that call, not on its own code. median = middle value (mean of the two middle ones for an even count), p95 = nearest rank. **Map** lights a
  run's files; **Copy slice** copies just the code it ran. Earlier recordings stay selectable (last 50), each labelled
  with the app it came from: folder name, port and start command (`shop-api · :8000 · uvicorn app.main:app`).
  Enabling tracing adds `faravex_trace.pth` to the app's venv (found in the folder, a parent or a direct
  subfolder; ignored in `.gitignore`): Python loads the probe at startup, idle until you record. A run starts where
  the framework calls into your code and includes async tasks and thread-pool work it hands off, so middleware,
  endpoint and a streamed response form one run. The probe is standalone (it never imports Faravex, so the app
  keeps its network) and writes next to the index. Cost on a FastAPI app: none while idle; recording adds about
  6 µs per project function call (1.4 → 2.4 ms for a request making ~150 calls).
  **Browser recording** (type a page URL instead of a port; a port in a folder without a Python venv, such as a
  static site or a JS dev server, records `http://localhost:<port>/`): Faravex opens Chrome or Edge with its own profile
  (kept in the index folder, reused across recordings; `FARAVEX_BROWSER` picks the executable) and records that
  tab over the DevTools protocol; nothing is added to the app. Runs show up live: the recording is read every second
  (a task running across a read shows as two runs), and it carries on when the tab moves to another site and back.
  Keep that window visible: hidden pages pause animation frames and observers, so little JS runs. The V8 sampling profiler (every 200 µs) gives the call
  timeline and self time; precise coverage gives exact call counts and each function's lines, so calls too short to
  be sampled still count and show up in the slice. A run is one main-thread task that entered your code (an event
  handler, a timer, a fetch callback), labelled by its first project function (`src/App.tsx:handleSubmit`; functions
  the code leaves nameless are named from their source line: `addEventListener("click") callback (line 364)`,
  `IIFE (line 15)`, `(top level)` for a script's own code); no
  inputs or outputs. Scripts map to project files through source maps (Vite and webpack dev builds, inline or by URL)
  or their URL path, so `.ts` / `.tsx` lines are the original ones. Limits: timings are sampled (consecutive calls of
  the same function merge into one bar; tasks with no idle gap between them merge into one run); production bundles
  without source maps aren't mapped; the recording lives in the Faravex process that started it (the shared MCP +
  UI server), so the CLI can't record a browser. CSS and HTML aren't traced.
  **Compare** two recordings of a folder (did my change make it slower?): pick one in **compare with…** next to the
  recordings list. Per entry point: runs, median and p95 base → this recording, the change and a verdict (*slower /
  faster* need ≥ 5 runs on each side and a median change of ≥ 10% and ≥ 0.5 ms; otherwise *same* or *few runs*;
  *new / gone* for entry points on one side only); click a row for where the time moved (each function's self time
  per run). Warns when the app, address or command differ. **Pin** keeps a recording (a baseline) from cleanup, which
  otherwise keeps the newest 50. Recordings never leave this machine (index folder, nothing in git). Agents:
  `trace action=compare session=<newer> base=<older>`; CLI: `server.py trace compare --session … --base …`.
  **Calls** (third rail button) lists every MCP tool call from any client with its time and rough token cost
  (~4 characters a token: *out* is the result the model reads, *in* the arguments it sent), plus totals per tool.
  Every server process appends to `indexes\calls.jsonl`; the MCP window prints the same line per call, and every
  MCP result starts with it (`[faravex: ~695 tokens out, ~21 in, 12 ms]`), so developers see each call's cost
  in their agent's chat. Results are plain text (no JSON-wrapped structured copy).
  **? Guide** (opens by itself the first time) walks through every feature with live "Try it" actions and ends
  with MCP setup snippets for Claude Code, VS Code / GitHub Copilot and Cline filled in with this machine's paths.

Air-gapped: HF offline env vars forced, `local_files_only=True`, and all non-loopback
sockets/DNS (TCP and UDP) blocked inside the process (`FARAVEX_ALLOW_NETWORK=1` disables the block).
The UI and MCP servers bind 127.0.0.1 only and reject non-loopback `Host` headers (DNS rebinding). The recording
browser runs with background networking, component updates, sync and pings off.

## New machine (Windows, no admin, no WSL)
1. `git clone` this repo.
2. Download the model (~1.5 GB, not in git): `hf download google/embeddinggemma-2 --local-dir models\embeddinggemma`
   (`pip install -U huggingface_hub` for `hf`), or put it elsewhere and set `FARAVEX_MODEL_PATH`.
3. Run `start.bat`. It:
   - downloads `uv` (one exe) into `.tools\` — falls back to an installed Python 3.10+ if github.com is blocked;
   - creates `.venv` with Python 3.13 (uv fetches it into your user profile if missing);
   - picks the torch build from the NVIDIA driver: R580+ → CUDA 13.0, R525+ → CUDA 12.6, else CPU;
   - installs `requirements.txt`, checks every version, loads the model once (smoke test);
   - opens the UI at http://127.0.0.1:8765 and prints the `claude mcp add` command for MCP use.
   Every run first checks what is really installed (`scripts\check_deps.py`: requirement versions, torch +
   torchvision importing together, CUDA visible) and installs only what is missing or broken; a healthy
   environment starts in ~3 s with no network. An existing `.venv` made by hand counts, including one created
   with `--system-site-packages` that reuses a system CUDA torch: pip handles that venv (uv ignores system
   packages and would pull PyPI's CPU-only torch into `.venv`, hiding the CUDA one).

Options: `start.bat -Mode check` (report only) · `-Mode setup` (no UI) · `-Python 3.12 -Force` (new venv on another
Python, for dependency conflicts) · `-Locked` (exact versions from `requirements-lock.txt`) · `-Torch cu126|cu130|cpu` ·
`-Port 8800` · `-NoBrowser`.

Small GPUs (e.g. 4 GB): the batch token budget is picked from free VRAM after the model loads (16384 / 8192 / 4096)
and halves itself on out-of-memory. Two processes (MCP + UI) can share one index (SQLite WAL).

## Model
Sentence-transformers folder at `models/embeddinggemma/` (EmbeddingGemma2: `config.json`, `modules.json`,
`model.safetensors`, `tokenizer.json`, `tokenizer_config.json`, `chat_template.jinja`, `processor_config.json`,
`1_Pooling/`, `2_Normalize/`, ...). Text encoder only is loaded (271M params). The processor also needs
`pillow` and `torchvision` installed even for text.

## Use
    start.bat                                                     # MCP :8766 + browser UI :8765 (sets up first if needed)
    .venv\Scripts\python.exe server.py ui [--port 8765] [--no-browser]
    .venv\Scripts\python.exe server.py index C:\path\to\repo     # pre-build index
    .venv\Scripts\python.exe server.py search "where auth tokens are checked" --path C:\path\to\repo
    .venv\Scripts\python.exe server.py outline faravex\index.py  # same output as the MCP tools
    .venv\Scripts\python.exe server.py deps FILE [--depth 2] [--path ROOT]
    .venv\Scripts\python.exe server.py impact FILE [--symbol NAME] [--depth 3] [--path ROOT]
    .venv\Scripts\python.exe server.py diff [--staged | --base main] [--path ROOT]
    .venv\Scripts\python.exe server.py trace enable|start|stop|show|status|disable|slice [--port 8000] [--path ROOT]
    .venv\Scripts\python.exe server.py                            # MCP stdio server
    .venv\Scripts\python.exe server.py http [--port 8766] [--ui-port 8765] [--browser]  # MCP over HTTP + UI, one shared process
    claude mcp add --transport http --scope user faravex http://127.0.0.1:8766/mcp       # connect Claude Code to it
    .venv\Scripts\python.exe tests\test_usage_lines.py            # tests (plain asserts; pytest works too)
    .venv\Scripts\python.exe tests\test_tracing.py

Indexes live in `indexes/` (SQLite, incremental by mtime/size, then sha1). Respects `.gitignore` and `.faravexignore`.

## Chunks
Python (`ast`), JS/TS (the outline scanner) and markdown (headings) are cut on function, class, method and
section boundaries; comments and decorators stay with their definition; small neighbours merge up to half the
chunk limits; long ones are line-split but keep their scope. Other languages use the line chunker. Each chunk's
scope (`class CodeIndex > def search`) is embedded as its title and shown in results. Changing the chunker
rebuilds indexes once (it is part of the index signature). 16-query eval, hybrid: right file first 14 -> 15 of 16,
file MRR 0.927 -> 0.953, exact chunk in top 3 unchanged (14/16).

## Hybrid search
Each index holds the chunk vectors and an FTS5 **trigram** keyword index over the same chunk text (case-insensitive
substring match: `HTTP_401_UNAUTHORIZED`, `401_UNAUTH`, a regex fragment or a UUID all hit). How they combine:
- query names something exact (snake_case, camelCase, CAPS, digits, `a.b`, quoted text, or a single word):
  reciprocal rank fusion with the keyword ranking weighted 3x, so chunks containing the name come first;
- plain question: cosine similarity decides, a keyword match adds at most 0.02 (breaks near-ties only;
  rank fusion let common words reorder clearly better semantic hits).

`mode`: `hybrid` (default) · `semantic` · `keyword` (BM25 only, no model, ~0.5 ms). Older indexes get the keyword
index built once from stored text on first open (no re-embedding; ~0.2 s per 1,000 chunks). Cost: the keyword index
is ~2.8x the indexed text (a codebase with 6.6 MB of text: 13.4 MB).

## Dependency map (UI)
Edges, all read from the files: **imports** (Python via `ast`; JS/TS `import`/`export from`/`require`/`import()`;
HTML `src`/`href`; CSS `@import`), **doc refs** (markdown links, `backticked` file names and `a/b/c.ext` paths in
docs and specs), and **semantic** doc → code links from the existing index (cosine ≥ 0.78, top 3 per doc, no model
call). The map works without an index; the **Index** button builds or updates it for semantic links.

## Measured (RTX 5060 Laptop GPU 8 GB, bf16, Windows 11, 2026-10-20)
Four real codebases, 20 questions each, timed inside the server (warm). *Context saved* = tokens of the search
results vs reading the files the hits are in whole.

| Codebase | Lines | Files | Chunks | Full index | Search p50 / p95 | Exact text p50 | Context saved |
|---|---|---|---|---|---|---|---|
| LLM training harness (Python + web UI) | 2.6k | 22 | 142 | 3.9 s | 9 / 10 ms | 1 ms | 8.7× |
| Static website (HTML/CSS/JS) | 6.1k | 37 | 257 | 6.9 s | 8 / 9 ms | 1 ms | 19× |
| Faravex itself (Python + JS) | 7.2k | 35 | 362 | 7.6 s | 8 / 10 ms | 1 ms | 11× |
| Full-stack web app (Python API + JS) | 32k | 100 | 1,718 | 53 s | 10 / 12 ms | 1 ms | 23× |

- Context saved grows with file size, not repo size: files averaging 2.1k tokens → 8.7×, 6.1k tokens → 23×.
- Outlines are 10–31× smaller than the files they describe.
- VRAM: 0.78 GB resident, the same after re-indexing all four (indexing's batch memory is handed back).
- Median MCP call: ~730 tokens out, 7 ms (142 logged calls).
- Queries replay a captured CUDA graph (2026-10-09: eager PyTorch 43.5 ms, ~90% of it CPU time launching ~2,500
  kernels; GPU compute was 4.8 ms).
- CPU only (2026-10-09): float32 is 4.3× faster than bf16 (70 chunks: 67 s vs 285 s), so CPU defaults to float32.

## Settings (env)
`FARAVEX_MODEL_PATH`, `FARAVEX_INDEX_DIR`, `FARAVEX_ROOT` (default cwd), `FARAVEX_HTTP_PORT` (8765), `FARAVEX_MCP_PORT` (8766, `http` mode),
`FARAVEX_DEVICE` (cuda/cpu), `FARAVEX_TEXT_ONLY` (1 = skip vision/audio encoders),
`FARAVEX_DTYPE` (empty = bfloat16 on CUDA, float32 on CPU; never float16), `FARAVEX_CUDA_GRAPHS` (1),
`FARAVEX_DIM` (768/512/256/128), `FARAVEX_MAX_SEQ_LEN` (8192), `FARAVEX_TOKEN_BUDGET` (0 = auto from VRAM),
`FARAVEX_BATCH_SIZE` (64 max), `FARAVEX_CHUNK_MAX_LINES` (60), `FARAVEX_CHUNK_MAX_CHARS` (2400),
`FARAVEX_QUERY_PROMPT`, `FARAVEX_DOC_PROMPT`, `FARAVEX_EXTRA_EXTS`.

## License
Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE). The bundled JetBrains Mono font is under the SIL Open
Font License 1.1. The model ([google/embeddinggemma-2](https://huggingface.co/google/embeddinggemma-2), Apache 2.0) is downloaded
separately, not included.
