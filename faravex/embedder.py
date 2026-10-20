"""Lazy, thread-safe wrapper around the local sentence-transformers model."""

import contextlib
import logging
import sys
import threading

import numpy as np

from . import config
from .files import CHUNKER

log = logging.getLogger("faravex")


def _read(path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


class Embedder:
    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self._budget = 16384
        self._graphs: dict[int, tuple] = {}  # bucket -> (graph, input_ids, attention_mask, output)
        self._graph_pool = None
        self._graphs_off = False

    @property
    def signature(self) -> str:
        """Identifies everything that changes embedding values; mismatch forces reindex."""
        cfg = config.MODEL_PATH / "config.json"
        stamp = cfg.stat().st_mtime_ns if cfg.exists() else 0
        return "|".join(map(str, (
            config.MODEL_PATH.resolve(), stamp, config.DIM, config.QUERY_PROMPT,
            config.DOC_PROMPT, config.CHUNK_MAX_LINES, config.CHUNK_MAX_CHARS, config.MAX_SEQ_LEN, CHUNKER,
        )))

    def _load(self):
        if self._model is not None:
            return self._model
        path = config.MODEL_PATH
        if not (path / "config.json").is_file():
            raise RuntimeError(
                f"Model not found at {path}. Download it there: hf download google/embeddinggemma-2 "
                f"--local-dir \"{path}\" (or set FARAVEX_MODEL_PATH to where it is)."
            )
        missing = [f for f in ("tokenizer_config.json",) if not (path / f).is_file()]
        sbert_cfg = path / "sentence_bert_config.json"
        if (sbert_cfg.is_file() and '"message"' in sbert_cfg.read_text(encoding="utf-8")
                and not (path / "chat_template.jinja").is_file() and not (path / "chat_template.json").is_file()
                and '"chat_template"' not in _read(path / "tokenizer_config.json")):
            missing.append("chat_template.jinja")
        if missing:
            raise RuntimeError(f"Model folder {path} is incomplete; copy {', '.join(missing)} from the same model release.")
        if not (path / "modules.json").is_file():
            log.warning("No modules.json in %s; sentence-transformers will fall back to mean pooling "
                        "without the Dense layers, which degrades EmbeddingGemma quality.", path)

        import torch
        from sentence_transformers import SentenceTransformer

        device = config.DEVICE or ("cuda" if torch.cuda.is_available() else "cpu")
        # Skip the unused vision/audio encoders (740M -> 270M params, identical text embeddings).
        model_cfg = _read(path / "config.json")
        config_kwargs = {k: None for k in ("vision_config", "audio_config")
                         if config.TEXT_ONLY and f'"{k}"' in model_cfg}
        dtype = config.DTYPE or ("bfloat16" if str(device).startswith("cuda") else "float32")
        log.info("Loading model from %s on %s (%s)", path, device, dtype)
        # Keep library chatter off stdout, which carries the MCP stdio protocol.
        with contextlib.redirect_stdout(sys.stderr):
            model = SentenceTransformer(
                str(path),
                device=device,
                local_files_only=True,
                truncate_dim=config.DIM,
                model_kwargs={"dtype": getattr(torch, dtype)},
                config_kwargs=config_kwargs,
            )
        if model.max_seq_length is None or model.max_seq_length > config.MAX_SEQ_LEN:
            model.max_seq_length = config.MAX_SEQ_LEN
        self._budget = config.TOKEN_BUDGET or self._auto_budget(torch, device)
        log.info("Token budget per batch: %d", self._budget)
        self._model = model
        return model

    @staticmethod
    def _auto_budget(torch, device: str) -> int:
        """Largest batch budget that fits the free VRAM after the model is loaded.

        Measured peak allocation at 16384 tokens: 2.5 GB (RTX 5060, bf16); it scales ~linearly.
        Halving the budget roughly halves indexing throughput, so only shrink when needed."""
        if not str(device).startswith("cuda"):
            return 16384
        free_gb = torch.cuda.mem_get_info()[0] / 2**30
        return 16384 if free_gb >= 4.5 else 8192 if free_gb >= 2.5 else 4096

    def warmup(self) -> None:
        """Import heavy libraries, load the model and run one encode on the calling thread.

        Must run on the main thread before the MCP stdio loop starts: on Windows, loading
        native DLLs (scipy/OpenBLAS, cuBLAS) from a worker thread while another thread is
        blocked reading the stdin pipe deadlocks.
        """
        import sentence_transformers  # noqa: F401  (pulls in torch, scipy, sklearn)

        try:
            self._encode(["warmup"])
            self.embed_query("warmup")  # captures the shortest CUDA graph bucket
            log.info("Model ready")
        except Exception as e:  # missing model etc.; tools will report the error
            log.error("Model warmup failed: %s", e)

    @staticmethod
    def _token_batches(lengths: list[int], budget: int) -> list[list[int]]:
        """Group indices, longest first, so longest_in_batch * batch_len <= budget."""
        batches: list[list[int]] = []
        batch: list[int] = []
        longest = 0
        for i in sorted(range(len(lengths)), key=lambda i: -lengths[i]):
            n = min(lengths[i], config.MAX_SEQ_LEN)
            if batch and (max(longest, n) * (len(batch) + 1) > budget
                          or len(batch) >= config.BATCH_SIZE):
                batches.append(batch)
                batch, longest = [], 0
            batch.append(i)
            longest = max(longest, n)
        if batch:
            batches.append(batch)
        return batches

    def _encode(self, texts: list[str]) -> np.ndarray:
        import torch

        with self._lock, contextlib.redirect_stdout(sys.stderr):
            model = self._load()
            lengths = [len(ids) + 2 for ids in model.tokenizer(texts, add_special_tokens=False)["input_ids"]]
            emb: np.ndarray | None = None
            while True:
                try:
                    for batch in self._token_batches(lengths, self._budget):
                        out = model.encode(
                            [texts[i] for i in batch],
                            prompt="",  # prompts are already applied; disable the model's default prompt
                            batch_size=len(batch),
                            convert_to_numpy=True,
                            normalize_embeddings=True,
                            show_progress_bar=False,
                        )
                        if emb is None:
                            emb = np.empty((len(texts), out.shape[1]), dtype=np.float32)
                        emb[batch] = out
                    break
                except torch.cuda.OutOfMemoryError:
                    if self._budget <= 1024:
                        raise
                    torch.cuda.empty_cache()
                    self._budget //= 2
                    log.warning("Out of GPU memory; token budget lowered to %d", self._budget)
            if len(texts) > 1 and torch.cuda.is_available():
                torch.cuda.empty_cache()  # hand indexing's batch peak back to other GPU apps; model + query graph stay
        emb /= np.linalg.norm(emb, axis=1, keepdims=True).clip(min=1e-12)
        return emb

    def _encode_query_graph(self, text: str) -> np.ndarray | None:
        """Single query through a captured CUDA graph (~10x faster than eager). None = use eager."""
        if not config.CUDA_GRAPHS or self._graphs_off:
            return None
        import torch

        with self._lock, contextlib.redirect_stdout(sys.stderr):
            model = self._load()
            if model.device.type != "cuda":
                return None
            n = len(model.tokenizer(text)["input_ids"])
            bucket = next((b for b in config.GRAPH_BUCKETS if b >= n), None)
            if bucket is None:
                return None
            try:
                entry = self._graphs.get(bucket) or self._capture(torch, model, bucket)
            except Exception as e:  # unsupported op inside capture etc.: stay on the eager path
                log.warning("CUDA graph capture failed (%s); using eager queries", e)
                self._graphs_off = True
                return None
            graph, ids, mask, out = entry
            tok = model.tokenizer([text], padding="max_length", max_length=bucket, truncation=True, return_tensors="pt")
            ids.copy_(tok["input_ids"])
            mask.copy_(tok["attention_mask"])
            graph.replay()
            vec = out[0, : config.DIM].float().cpu().numpy() if config.DIM else out[0].float().cpu().numpy()
        return vec / max(float(np.linalg.norm(vec)), 1e-12)

    def _capture(self, torch, model, bucket: int) -> tuple:
        ids = torch.zeros((1, bucket), dtype=torch.long, device=model.device)
        mask = torch.zeros((1, bucket), dtype=torch.long, device=model.device)
        feats = {"input_ids": ids, "attention_mask": mask}
        if self._graph_pool is None:
            self._graph_pool = torch.cuda.graph_pool_handle()  # replays are serialized, so buckets share memory
        side = torch.cuda.Stream()
        side.wait_stream(torch.cuda.current_stream())
        with torch.inference_mode():
            with torch.cuda.stream(side):
                for _ in range(3):
                    model(feats)
            torch.cuda.current_stream().wait_stream(side)
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph, pool=self._graph_pool):
                out = model(feats)["sentence_embedding"]
        self._graphs[bucket] = (graph, ids, mask, out)
        log.info("Captured CUDA graph for queries up to %d tokens", bucket)
        return self._graphs[bucket]

    def embed_documents(self, items: list[tuple[str, str]]) -> np.ndarray:
        """items: (title, text) pairs."""
        return self._encode([config.DOC_PROMPT.format(title=t) + text for t, text in items])

    def embed_query(self, query: str) -> np.ndarray:
        text = config.QUERY_PROMPT + query
        vec = self._encode_query_graph(text)
        return vec if vec is not None else self._encode([text])[0]
