"""Settings, all overridable through FARAVEX_* environment variables."""

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default):
    value = os.environ.get(f"FARAVEX_{name}")
    if value is None or value == "":
        return default
    return type(default)(value) if default is not None else value


# Local model folder in sentence-transformers format (modules.json, config.json,
# model.safetensors, tokenizer files, 1_Pooling/, 2_Dense/, ...). Never downloaded.
MODEL_PATH = Path(_env("MODEL_PATH", str(APP_DIR / "models" / "embeddinggemma")))
INDEX_DIR = Path(_env("INDEX_DIR", str(APP_DIR / "indexes")))
CALLS_LOG = INDEX_DIR / "calls.jsonl"  # every MCP tool call: time and rough token cost (UI: Calls)
CALLS_LOG_MAX = _env("CALLS_LOG_MAX", 2_000_000)  # bytes; past it the oldest half of the calls is dropped

DEVICE = _env("DEVICE", None)  # None = cuda if available, else cpu
# Empty = bfloat16 on CUDA (2x faster than float32), float32 on CPU (most CPUs have no bf16 math: 4.3x faster
# than bf16 there). Never float16: EmbeddingGemma does not support it.
DTYPE = _env("DTYPE", "")
TEXT_ONLY = _env("TEXT_ONLY", "1") == "1"  # don't load vision/audio encoders
# Matryoshka truncation: 768 (full), 512, 256 or 128. Only saves index storage
# (768-d = 3 KB/chunk); costs quality, so keep full unless the index gets huge.
DIM = _env("DIM", 0) or None
# Truncation ceiling per chunk (model trained to ~8k). Long sequences cost more per token
# (full-attention layers are quadratic), so chunk size, not this, sets indexing speed.
MAX_SEQ_LEN = _env("MAX_SEQ_LEN", 8192)
# Batches are packed so (longest sequence x batch size) stays under this many tokens.
# 16384 peaks at ~3.6 GB VRAM in bf16 at any length from 1k to 8k.
# 0 = pick from free VRAM at load (16384 / 8192 / 4096); halved automatically on out-of-memory.
TOKEN_BUDGET = _env("TOKEN_BUDGET", 0)
BATCH_SIZE = _env("BATCH_SIZE", 64)  # upper bound on chunks per batch
# Replay queries through CUDA graphs: eager PyTorch spends ~90% of a single-query encode
# launching ~2,500 small kernels on the CPU; a captured graph launches them as one.
CUDA_GRAPHS = _env("CUDA_GRAPHS", "1") == "1"
GRAPH_BUCKETS = (32, 64, 128, 256, 512)  # padded query lengths, one graph each, captured on first use

# Chrome or Edge executable for browser recording; empty = find an installed one.
BROWSER = _env("BROWSER", "")

# Browser UI (python server.py ui). Loopback only.
HTTP_PORT = _env("HTTP_PORT", 8765)
# MCP over HTTP (python server.py http): one shared process for every client, plus the UI. Loopback only.
MCP_PORT = _env("MCP_PORT", 8766)

# EmbeddingGemma prompt formats for code retrieval. {title} is the file path.
QUERY_PROMPT = _env("QUERY_PROMPT", "task: code retrieval | query: ")
DOC_PROMPT = _env("DOC_PROMPT", "title: {title} | text: ")

CHUNK_MAX_LINES = _env("CHUNK_MAX_LINES", 60)
CHUNK_MAX_CHARS = _env("CHUNK_MAX_CHARS", 2400)
CHUNK_OVERLAP = _env("CHUNK_OVERLAP", 8)
MAX_FILE_BYTES = _env("MAX_FILE_BYTES", 1_000_000)
EXTRA_EXTS = {e.strip().lower() for e in _env("EXTRA_EXTS", "").split(",") if e.strip()}

# Default root when a tool call omits `path`.
DEFAULT_ROOT = Path(_env("ROOT", os.getcwd()))
