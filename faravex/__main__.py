"""CLI: python -m faravex [serve | http | ui | index PATH | search QUERY | status | outline FILE | deps FILE | impact FILE | diff
| trace ACTION]"""

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="faravex", description="Offline code intelligence: search, map, record")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run MCP stdio server (default)")
    p = sub.add_parser("index", help="build/update index for a project")
    p.add_argument("path", nargs="?")
    p.add_argument("--full", action="store_true", help="rebuild from scratch")
    p = sub.add_parser("search", help="search a project")
    p.add_argument("query")
    p.add_argument("--path")
    p.add_argument("-k", "--top-k", type=int, default=8)
    p.add_argument("--glob")
    p.add_argument("--max-lines", type=int, default=25)
    p.add_argument("--mode", choices=("hybrid", "semantic", "keyword"), default="hybrid")
    p = sub.add_parser("status", help="show index status")
    p.add_argument("path", nargs="?")
    p = sub.add_parser("outline", help="classes / functions / signatures of a file")
    p.add_argument("file")
    p.add_argument("--path")
    p = sub.add_parser("deps", help="imports, importers and docs of a file")
    p.add_argument("file")
    p.add_argument("--path")
    p.add_argument("--depth", type=int, default=1)
    p = sub.add_parser("impact", help="blast radius: files and tests a change to a file (or symbol) can break")
    p.add_argument("file")
    p.add_argument("--symbol")
    p.add_argument("--path")
    p.add_argument("--depth", type=int, default=3)
    p = sub.add_parser("diff", help="what uncommitted changes touch: usages, similar code, tests")
    p.add_argument("--path")
    p.add_argument("--staged", action="store_true")
    p.add_argument("--base")
    p.add_argument("-k", "--top-k", type=int, default=5)
    p = sub.add_parser("trace", help="live execution trace of a running Python 3.12+ app (slice = source of what ran; "
                                     "compare = two recordings: --session newer --base older)")
    p.add_argument("action", choices=("enable", "start", "stop", "show", "compare", "status", "disable", "slice"))
    p.add_argument("--port", type=int)
    p.add_argument("--path")
    p.add_argument("--session")
    p.add_argument("--base")
    p.add_argument("--run")
    p.add_argument("--max-chars", type=int, default=24000)
    p = sub.add_parser("ui", help="browser UI with the dependency map (http://127.0.0.1:8765)")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true")
    p = sub.add_parser("http", help="MCP over HTTP (http://127.0.0.1:8766/mcp) + browser UI in one shared process")
    p.add_argument("--port", type=int)
    p.add_argument("--ui-port", type=int)
    p.add_argument("--browser", action="store_true", help="open the UI in the browser")
    args = parser.parse_args(argv)

    # stdout is reserved for the MCP protocol; all logging goes to stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("httpx", "sentence_transformers", "transformers", "mcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.cmd == "ui":
        from . import web

        web.serve(args.port, open_browser=not args.no_browser)
        return

    from . import server

    if args.cmd in (None, "serve"):
        return server.serve()
    if args.cmd == "http":
        return server.serve_http(args.port, args.ui_port, args.browser)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # file text may hold any Unicode
    if args.cmd == "index":
        print(server.index_codebase(args.path, args.full))
    elif args.cmd == "search":
        print(server.search_code(args.query, args.path, args.top_k, args.glob, args.max_lines, True, args.mode))
    elif args.cmd == "status":
        print(server.index_status(args.path))
    elif args.cmd == "outline":
        print(server.get_symbol_outline(args.file, args.path))
    elif args.cmd == "deps":
        print(server.get_dependencies(args.file, args.path, args.depth))
    elif args.cmd == "impact":
        print(server.get_blast_radius(args.file, args.symbol, args.path, args.depth))
    elif args.cmd == "diff":
        print(server.search_diff(args.path, args.staged, args.base, args.top_k))
    elif args.cmd == "trace" and args.action == "slice":
        print(server.get_execution_slice(args.path, args.session, args.max_chars, args.run))
    elif args.cmd == "trace":
        print(server.trace(args.action, args.port, args.path, args.session, args.run, base=args.base))


if __name__ == "__main__":
    main()
