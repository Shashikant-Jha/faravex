# Faravex features

Faravex indexes a codebase on your machine with a local embedding model (EmbeddingGemma2). It gives AI coding
agents, and you, faster ways to find and understand code than grep and reading whole files. It has two front doors
onto one engine and one index:

- **MCP tools** for any MCP client: Claude Code, GitHub Copilot in VS Code, Cline and others.
- **A browser UI** at http://127.0.0.1:8765: dependency map, search, git changes, live recording, tool-call costs.

Everything runs locally. Your code, indexes and recordings never leave the machine (see
[Privacy and air-gapping](#privacy-and-air-gapping)).

This file explains every feature and how to use it. [README.md](README.md) covers installation details, internals,
measurements and settings.

**Contents**

1. [Getting started](#getting-started)
2. [Search](#search)
3. [Navigating code](#navigating-code)
4. [Checking your changes](#checking-your-changes)
5. [Dependency map](#dependency-map)
6. [Recording what runs](#recording-what-runs)
7. [Comparing recordings](#comparing-recordings)
8. [Token cost per call](#token-cost-per-call)
9. [Guide](#guide)
10. [The index](#the-index)
11. [Privacy and air-gapping](#privacy-and-air-gapping)
12. [Limits](#limits)

---

## Getting started

### Install (Windows; no admin rights, no WSL)

1. Clone this repository.
2. Download the model, [google/embeddinggemma-2](https://huggingface.co/google/embeddinggemma-2) (Apache 2.0, about
   1.5 GB), into `models\embeddinggemma\`:

       hf download google/embeddinggemma-2 --local-dir models\embeddinggemma

   `hf` comes with `pip install -U huggingface_hub`; after step 3 it is also in `.venv\Scripts\`. To keep the model
   somewhere else, point `FARAVEX_MODEL_PATH` at its folder. Faravex itself never downloads anything: this is the
   one step that needs the internet, besides installing packages.
3. Run `start.bat`. On the first run it:
   - sets up `.venv`;
   - picks the PyTorch build that matches your GPU, or CPU;
   - installs the dependencies and checks them;
   - starts Faravex.

   Later runs only check what's installed and start in a few seconds.

`start.bat` starts one process that serves both front doors, in a window titled `Faravex MCP :8766 + UI :8765`:

- MCP at `http://127.0.0.1:8766/mcp`
- the UI at `http://127.0.0.1:8765`

Close the window to stop it. If it's already running, `start.bat` just opens the UI.

The first search after a start can wait 10 to 75 seconds while the model loads onto the GPU.

### Connect an AI agent

Claude Code:

    claude mcp add --transport http --scope user faravex http://127.0.0.1:8766/mcp

Then run `/mcp` in Claude Code to connect.

For VS Code (GitHub Copilot) and Cline, open the UI's **? Guide**. Its last step shows the config snippet for each
client, filled in with this machine's paths.

You can also run Faravex as a stdio server: `.venv\Scripts\python.exe server.py`. Each client then starts its own
copy and loads its own model. HTTP lets every client share one process.

### Tell your agent to use it

Agents use tools they're told about. A line like this in your agent instructions (`CLAUDE.md`,
`.github/copilot-instructions.md`, `.clinerules`) works well:

> Read code through the Faravex MCP: `search_code` to find code, `get_symbol_outline` before opening a file,
> `get_context_slice` for one file or function with what it uses, `get_blast_radius` before editing shared code,
> `search_diff` after a change. Read only the line ranges these tools return.

Every tool takes `path`, the project root. You can leave it out when `file` is an absolute path: Faravex then
uses the enclosing git repository as the root.

---

## Search

**MCP:** `search_code(query, path, top_k=8, glob, max_lines=25, refresh=True, mode="hybrid")`
**UI:** the search box above the map
**CLI:** `server.py search "query" --path C:\repo`

Search finds code by what it does and by exact text, in one query:

- **By meaning:** "where JWT tokens are validated", "retry logic for HTTP calls". These find the right code even
  when it shares no words with the question.
- **By exact text:** `HTTP_401_UNAUTHORIZED`, `parseConfig`, `"connection refused"`, part of a UUID or a regex
  fragment. Matching is case-insensitive, needs 3 or more characters, and finds the text anywhere inside words.
- **A mix of both:** "where parseConfig handles missing files".

Each result is a `file:start-end` line range with a snippet and the enclosing scope (`class CodeIndex > def search`).
The agent then reads only those lines instead of whole files.

How the two rankings combine:

- When the query names something exact (snake_case, camelCase, CAPS, digits, `a.b`, quoted text or a single word),
  chunks that contain the name come first.
- When the query is a plain question, meaning decides. An exact-text match only breaks near-ties.

Options:

| Option | What it does |
|---|---|
| `mode="keyword"` | Exact text only, ranked by BM25. No model call, about 0.5 ms. Use it to list where a name appears. |
| `mode="semantic"` | Meaning only. |
| `glob="src/api/*,*.ts"` | Search only matching files. |
| `max_lines=0` | Locations only, no snippets. The cheapest output. |
| `refresh=False` | Skip re-embedding files changed since the last search. |

Searches keep the index current: files changed since the last call are re-embedded first, which is fast when only a
few changed.

In the UI, matching files light up on the map, numbered by rank. The import paths between them are highlighted, so
you can see whether the hits form one subsystem or several. Click a result to open the file at the matched lines.

---

## Navigating code

These tools replace "open the file and read it". Each returns a fraction of the tokens a full read costs.

### Outline of a file

**MCP:** `get_symbol_outline(file, path)` · **CLI:** `server.py outline FILE`

Lists a file's classes, functions and methods with their signatures, first docstring line and start-end line numbers.

| File type | How it's outlined |
|---|---|
| Python | `ast` |
| JS/TS | a brace-aware scanner |
| Markdown | headings |
| JSON, YAML, TOML | top-level keys |
| Other languages | heuristic |

Typical use: outline the file, then read only the function you need.

### Dependencies

**MCP:** `get_dependencies(file, path, depth=1)` · **CLI:** `server.py deps FILE [--depth 2]`

Shows four things for one file:

- what it imports;
- what imports it, to any depth;
- which docs name it or link to it;
- its external packages.

It also lists "semantically linked" docs: specs and READMEs whose content is close to the file even though they never
name it. Tests are marked `[test]`.

### Blast radius

**MCP:** `get_blast_radius(file, symbol, path, depth=3)` · **CLI:** `server.py impact FILE [--symbol NAME]`

Answers "what can this change break?" before you edit shared code:

- every file that imports the file, directly or through other files, by hop count;
- the tests among them;
- the docs that describe it.

With `symbol` (a function, class or constant defined in the file), it also gives:

- the exact lines in those files that use the symbol;
- the direct importers that never use it, which a rename or signature change won't affect.

### Context slice

**MCP:** `get_context_slice(file, symbol, path, max_chars=24000)` · **UI:** **Copy context** in the side panel

One paste-ready bundle for working on a file or a single function. It holds:

- its source (only the function, when `symbol` is given);
- the outlines of the files it imports;
- the lines in other files that call it;
- the doc lines that mention it.

When the bundle would exceed `max_chars`, sections are dropped and listed so nothing is cut silently. In the UI,
**Copy context** puts the same bundle on the clipboard, for chats that have no MCP.

### Import graph scope

Import edges are read for Python, JavaScript/TypeScript, HTML (`src`, `href`) and CSS (`@import`).

Files in other languages are fully searchable and outlined. The dependency tools find no importers for them, and say
so in a `note:` line; use `search_code` to find their usages.

---

## Checking your changes

**MCP:** `search_diff(path, staged=False, base, top_k=5)`
**UI:** **Changes** button on the map
**CLI:** `server.py diff [--staged | --base main]`

Reads your git changes and, for each changed region, shows:

- the function or class it's in;
- the lines in other files that use the changed names (callers to update);
- the most similar code in other files (duplicates that may need the same fix);
- the tests likely affected, found through imports, usages or similarity.

By default it covers uncommitted changes plus untracked files. Two variants:

- `staged=True` covers only staged changes.
- `base="main"` compares the working tree with a branch or commit.

Run it after an edit, before you commit. In the UI, changed files show as **Δ** on the map, with the related files
numbered around them.

---

## Dependency map

Open the UI, pick a folder with **Browse** (or type its path) and press Enter. The map draws the project from its files.

**Edges, all read from the files:**

- **imports**: Python, JS/TS `import` / `export from` / `require` / `import()`, HTML `src` / `href`, CSS `@import`;
- **doc refs**: markdown links, backticked file names and `a/b/c.ext` paths in docs and specs;
- **semantic**: doc → code links from the search index (no model call).

The map works without an index. **Index** builds or updates the index, which adds the semantic links and enables
search.

**Using it:**

- Click a file to open the side panel: summary, imports, importers, docs, packages, how many files it affects, and a
  code preview.
- Drag nodes to untangle dense areas. Positions are kept per project; **Reset layout** forgets them.
- The filter box highlights files by name; Enter jumps to the first match.
- Toggles show or hide each edge kind (imports, doc refs, semantic) and unlinked files.

The UI fits laptop and desktop windows from about 960 px wide up to 2560 px; it is not designed for phones.

---

## Recording what runs

Static tools show what *could* run. Recording shows what *did* run for one action: which project functions ran, in
what order, how long each took, and where the time went. Then `get_execution_slice` returns the source of only
those functions. That is often the smallest context an agent needs to fix a bug in that action.

Faravex records two kinds of app:

- Python web apps (3.12+), picked by port;
- web pages, which record the JavaScript/TypeScript running in the browser.

### Python apps

1. **Enable once per project.** In the UI's Record view, type the app's port (for example `8000`) and press
   **Record**. The first time, Faravex asks to enable tracing; agents call `trace action=enable path=…`.
   This adds `faravex_trace.pth` to the app's virtual environment, which is found in the folder, a parent folder
   or a direct subfolder. The probe stays idle until you record and costs nothing while idle.
2. **Restart the app** so Python loads the probe.
3. **Record.** Press **Record** again; agents call `trace action=start port=8000`.
4. **Use the app:** click through the UI or call the endpoints. Runs appear live, about once a second.
5. Press **Stop** (`trace action=stop`). Agents get the latency report.

`trace action=disable` removes the probe.

**What counts as a run:** one request, starting where the framework calls into your code. It includes async tasks and
thread-pool work the request hands off, so middleware, the endpoint and a streamed response form one run.

**Overhead:** about 6 µs per project function call while recording (one request making ~150 calls went from 1.4 ms to
2.4 ms).

**Inputs:** each run keeps its entry arguments, the request body and the result, all trimmed. Values named password,
token, secret, api_key, authorization or cookie are masked.

The probe is standalone: it never imports Faravex, so the app keeps its own network access. It writes recordings
next to the index.

### Web pages (browser recording)

Type a page URL into the Record box, for example `http://localhost:5173/`, and press **Record**. Agents call
`trace action=start url=…`. No enable step, and nothing is added to the app.

A port in a folder without a Python venv (a static site, or a JS dev server) is recorded as the page
`http://localhost:<port>/`.

**What happens:**

- Faravex opens Chrome or Edge in a separate window, with its own browser profile kept in the index folder.
- It records that tab over the DevTools protocol. Your everyday browser windows are not recorded.
- Chrome refuses DevTools connections on your everyday profile, which is why the window is separate.
- `FARAVEX_BROWSER` sets which browser executable to use.

**Use the page in that window.** Runs show up within a second, and recording carries on when the tab moves to another
site and back. Keep the window visible: hidden pages pause animation frames and observers, so little JavaScript runs.

**How it measures:**

- A run is one main-thread task that entered your code: an event handler, a timer, a fetch callback.
- Each run is labelled by its first project function, such as `src/App.tsx:handleSubmit`.
- Nameless functions are named from their source line: `addEventListener("click") callback (line 364)`,
  `IIFE (line 15)`, `(top level)`.
- A sampling profiler (every 200 µs) gives the call timeline and self time.
- Precise coverage gives exact call counts, so calls too short to be sampled still count.
- Source maps (Vite and webpack dev builds, inline or by URL) map bundled code back to your `.ts` / `.tsx` lines.

### Reading the Record view

- Runs are grouped by entry point (`POST /api/orders`, `src/App.tsx:handleSubmit`), each with its run count, median,
  p95 and max time.
- Each run is a card. Open it for the call timeline. Each row is one call:
  - the number is how long the call took, call to return;
  - the bright part of the bar is **self** time: time in the function's own code, including library calls and
    network waits inside it;
  - the dim part is time waiting on the project calls listed under it.
- The yellow number marks the call with the most self time in that run: the bottleneck.
- Calls under 0.5% of the run fold away.
- **Map** lights up the files a run touched. **Copy slice** copies only the code it ran.
- Earlier recordings stay in the list, labelled with the app, port and start command. The newest 50 are kept, plus
  pinned ones.

### For agents

| Call | Returns |
|---|---|
| `trace action=stop` | Latency report: per entry point, runs, median / p95 / max, where the time goes, calls across files, functions in those files that did not run |
| `trace action=show` | The latest recording (or `session=…`); while recording, what ran so far |
| `trace action=show run=12` | One run's call timeline with its inputs and output |
| `trace action=status` | Whether tracing is enabled, what is recording, and the kept recordings with their ids |
| `get_execution_slice(path, session, run, max_chars)` | Source of only the functions that ran, grouped by file, with call counts and self time |

CLI: `server.py trace enable|start|stop|show|status|disable|slice [--port 8000] [--path ROOT]`.

The CLI can record Python apps but not browsers, because a browser recording lives inside the server process.

---

## Comparing recordings

Did my change make it slower? Record an action, change the code, record the same action again, then compare.

**In the UI (Record view):**

1. Pick a recording in the recordings list.
2. Pick another one in **compare with…** next to it.
3. A table appears above the run cards.

Each row of the table is one entry point and shows:

- the run count on each side;
- median and p95, other recording → this one;
- the change in ms and %;
- a verdict.

Verdicts:

| Verdict | When |
|---|---|
| **slower** / **faster** | Both sides have at least 5 runs, and the median moved by at least 10% and at least 0.5 ms |
| **same** | Enough runs, but the change is below those thresholds |
| **few runs** | Fewer than 5 runs on a side. The numbers show, but no verdict is given |
| **new** / **gone** | The entry point ran on one side only (a code path added or removed) |

Click a row to see **where the time moved**: each function's self time per run, with the biggest increases and
decreases.

A warning appears when the two recordings come from a different app, address or command, because the comparison may
not be like for like.

**Pin** keeps a recording, such as a baseline, from being cleaned up. Unpinned recordings beyond the newest 50 are
deleted.

**For agents:** `trace action=status` lists recording ids, then call
`trace action=compare session=<newer> base=<older>`. Regressions come first.

**CLI:** `server.py trace compare --session <newer> --base <older>`.

Recordings stay in the local index folder. They are never committed, uploaded or shared.

**For a fair comparison:**

- repeat the same actions in both recordings;
- get at least 5 runs per entry point;
- compare recordings of the same app and address.

---

## Token cost per call

Every MCP result starts with a header line:

    [faravex: ~695 tokens out, ~21 in, 12 ms]

So you can see each call's cost in your agent's chat. The counts are estimates at about 4 characters per token:

- **out** is the result the model reads;
- **in** is the arguments it sent.

Results are plain text, not JSON-wrapped, so nothing is paid twice.

The UI's **Calls** view (third button on the left rail) lists every tool call from every client: time, tokens and
arguments, plus totals per tool, costliest first. Use it to see which tools your agent leans on and what they cost.

The log is `indexes\calls.jsonl`. It trims itself to its newest half past 2 MB (`FARAVEX_CALLS_LOG_MAX`).

---

## Guide

**? Guide** in the UI header opens an interactive tour of every feature, with "Try it" buttons that run each one on
your folder. It opens by itself the first time. Its last step has the MCP setup snippets for Claude Code,
VS Code / GitHub Copilot and Cline.

---

## The index

- **Where:** `indexes/` in the Faravex folder, one SQLite database per project. Change the location with
  `FARAVEX_INDEX_DIR`.
- **What:**
  - source, docs and config files;
  - respects `.gitignore`, plus a `.faravexignore` in the project for anything else to skip;
  - skips binary files and files over 1 MB.
- **How it's cut:** Python, JS/TS and markdown are cut at function, class and section boundaries, with comments and
  decorators kept with their definition. Other files are cut by lines.
- **Updates:** incremental. Files are compared by modification time and size, then content hash, and only changed
  files are re-embedded. Searches refresh it automatically; `index_codebase(full=True)` rebuilds from scratch.
- **Two kinds of index:** each index holds the embedding vectors and a trigram keyword index over the same text.
- **Sharing:** several processes can share one index safely.

Small GPUs work. The batch size is picked from free GPU memory and halves itself on out-of-memory. CPU-only machines
work too, more slowly.

---

## Privacy and air-gapping

Faravex is built to run with no network.

- **Model:** loaded only from the local folder (`local_files_only`, Hugging Face offline mode, telemetry off).
- **Network blocked inside the process:** DNS lookups and connections to anything but this machine are refused,
  including UDP. `FARAVEX_ALLOW_NETWORK=1` turns this off, but nothing in Faravex needs it.
- **Local-only servers:**
  - Both servers listen on 127.0.0.1 only, so other machines can't reach them.
  - Both reject requests whose `Host` header isn't loopback, which blocks DNS-rebinding attacks from web pages.
  - The UI accepts changes only as JSON POSTs, which other websites can't send to it.
- **Recordings and logs stay local:** recordings, call logs and indexes live in `indexes/` and are never committed or
  uploaded. Recorded inputs have secret-looking values masked.
- **The recording browser:** it runs with Chrome's background networking, component updates, sync and pings turned
  off, so it only loads the page you give it.
- **Setup is the exception:** `start.bat` uses the network to install Python packages (and `uv`, unless github.com is
  blocked). After setup, a healthy start needs no network.

---

## Limits

- **Platform:** setup (`start.bat`) is Windows-only. The Python code is cross-platform, but on other systems you
  create the virtual environment and install `requirements.txt` yourself.
- **Python recording:** Python 3.12+ only, because it uses `sys.monitoring`. Other backends (Node, Java, .NET) aren't
  recorded yet.
- **Browser recording:**
  - timings are sampled, so back-to-back calls of one function merge into one bar, and tasks with no idle gap merge
    into one run;
  - no inputs or outputs per run;
  - production bundles without source maps aren't mapped;
  - CSS and HTML aren't traced;
  - restarting Faravex ends a running browser recording.
- **Import graph:** Python and JS/TS only (see [Import graph scope](#import-graph-scope)).
- **Fonts:** the UI's display font (Satoshi) can't be redistributed, so this repository leaves it out and the UI uses
  your system font. To get it, put `satoshi-var.woff2` from fontshare.com in `faravex/web/fonts/`.
