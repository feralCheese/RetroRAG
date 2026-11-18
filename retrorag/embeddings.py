"""Embedding providers. All return L2-normalised float32 numpy arrays.

  builtin   - (default) embedding model running inside RetroRAG via ONNX Runtime: no server, works offline after a
              one-time download (nomic-embed-text 137 MB, or bge-small 34 MB), on Windows / macOS / Linux / Pi
  ollama    - local Ollama server (/api/embed), e.g. nomic-embed-text, mxbai-embed-large, qwen3-embedding
  openai    - any OpenAI-compatible /v1/embeddings endpoint: Unsloth Studio, LM Studio, llama.cpp server,
              LocalAI, vLLM, OpenAI (API key sent as Authorization: Bearer ...)
  fastembed - in-process ONNX models (pip install fastembed), no server needed
  none      - keyword (BM25) search only
"""
from __future__ import annotations

import numpy as np
import httpx

from .config import LibrarySettings


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    signature = "none"

    def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    def _norm(v: np.ndarray) -> np.ndarray:
        v = np.asarray(v, dtype=np.float32)
        n = np.linalg.norm(v, axis=1, keepdims=True)
        n[n == 0] = 1
        return v / n


class OllamaEmbedder(Embedder):
    def __init__(self, s: LibrarySettings):
        self.url = (s.embed_url or "http://localhost:11434").rstrip("/")
        self.model, self.s = s.embed_model, s
        self.signature = s.embed_signature()

    def embed(self, texts, is_query=False):
        pre = self.s.embed_query_prefix if is_query else self.s.embed_doc_prefix
        try:
            r = httpx.post(f"{self.url}/api/embed", json={"model": self.model, "input": [pre + t for t in texts],
                                                          "truncate": True}, timeout=300)
        except httpx.HTTPError as e:
            raise EmbeddingError(f"Cannot reach Ollama at {self.url}: {e}") from e
        if r.status_code == 404 and "not found" in r.text:
            raise EmbeddingError(f"Ollama model '{self.model}' is not installed. Run:  ollama pull {self.model}")
        if r.status_code != 200:
            raise EmbeddingError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return self._norm(r.json()["embeddings"])


class OpenAIEmbedder(Embedder):
    def __init__(self, s: LibrarySettings):
        self.base = (s.embed_url or "http://localhost:1234/v1").rstrip("/")
        self.model, self.key, self.s = s.embed_model, s.embed_api_key, s
        self.signature = s.embed_signature()

    def embed(self, texts, is_query=False):
        pre = self.s.embed_query_prefix if is_query else self.s.embed_doc_prefix
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        try:
            r = httpx.post(f"{self.base}/embeddings", headers=headers,
                           json={"model": self.model, "input": [pre + t for t in texts], "encoding_format": "float"},
                           timeout=300)
        except httpx.HTTPError as e:
            raise EmbeddingError(f"Cannot reach {self.base}: {e}") from e
        if r.status_code == 401:
            raise EmbeddingError(f"{self.base} rejected the API key (401). Unsloth Studio: Settings > API > Create key.")
        if r.status_code != 200:
            raise EmbeddingError(f"Embedding endpoint error {r.status_code}: {r.text[:300]}")
        data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        return self._norm([d["embedding"] for d in data])


class FastEmbedEmbedder(Embedder):
    _models: dict = {}

    def __init__(self, s: LibrarySettings):
        try:
            from fastembed import TextEmbedding  # noqa: F401
        except ImportError as e:
            raise EmbeddingError("fastembed is not installed:  pip install fastembed") from e
        self.model_name = s.embed_model or "BAAI/bge-small-en-v1.5"
        self.s = s
        self.signature = s.embed_signature()

    def _model(self):
        from fastembed import TextEmbedding
        if self.model_name not in self._models:
            self._models[self.model_name] = TextEmbedding(self.model_name)
        return self._models[self.model_name]

    def embed(self, texts, is_query=False):
        m = self._model()
        vecs = list(m.query_embed(texts) if is_query else m.passage_embed(texts))
        return self._norm(np.vstack(vecs))


# ============================================================================ built-in (no server needed)
BUILTIN_DEFAULT = "nomic-embed-text-v1.5"   # accuracy of Ollama's nomic, ~2x faster, no server
BUILTIN_MODELS = {
    # BAAI bge-small-en-v1.5 (MIT), int8-quantised ONNX export: 34 MB, 384 dimensions, strong retrieval for its size
    "bge-small-en-v1.5": {
        "files": {"model.onnx": "https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/onnx/model_quantized.onnx",
                  "tokenizer.json": "https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/tokenizer.json"},
        "pooling": "cls", "max_len": 512, "size_mb": 34,
        "query_prefix": "Represent this sentence for searching relevant passages: ", "doc_prefix": "",
    },
    # nomic-embed-text v1.5 (Apache-2.0) - the same model Ollama serves, run in-process: 137 MB int8, 768 dims
    "nomic-embed-text-v1.5": {
        "files": {"model.onnx": "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5/resolve/main/onnx/model_quantized.onnx",
                  "tokenizer.json": "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5/resolve/main/tokenizer.json"},
        "pooling": "mean_ln", "max_len": 512, "size_mb": 137,
        "query_prefix": "search_query: ", "doc_prefix": "search_document: ",
    },
}


_ALIASES = {
    "nomic-embed-text": "nomic-embed-text-v1.5", "nomic-embed-text:latest": "nomic-embed-text-v1.5",
    "nomic-embed-text:v1.5": "nomic-embed-text-v1.5", "nomic-ai/nomic-embed-text-v1.5": "nomic-embed-text-v1.5",
    "nomic": "nomic-embed-text-v1.5",
    "bge-small": "bge-small-en-v1.5", "baai/bge-small-en-v1.5": "bge-small-en-v1.5", "bge-small-en": "bge-small-en-v1.5",
}


def resolve_builtin(name: str | None) -> str | None:
    """Canonical built-in model name for what the user typed ('' -> default; Ollama-style names accepted);
    None if it isn't a model RetroRAG can run itself."""
    n = (name or "").strip()
    if not n:
        return BUILTIN_DEFAULT
    if n in BUILTIN_MODELS:
        return n
    return _ALIASES.get(n.lower())


def model_dirs(name: str) -> list:
    """Where a built-in model may live: shipped next to the app (offline / standalone builds), else the user cache."""
    from .config import app_dir, app_root
    return [app_root() / "models" / name, app_dir() / "models" / name]


def find_model(name: str):
    for d in model_dirs(name):
        if all((d / f).is_file() for f in BUILTIN_MODELS[name]["files"]):
            return d
    return None


def ensure_model(name: str = BUILTIN_DEFAULT, target=None, log=print):
    """Return the folder holding the model, downloading it once (~34 MB) if needed."""
    from pathlib import Path
    if name not in BUILTIN_MODELS:
        raise EmbeddingError(f"Unknown built-in model '{name}'. Available: {', '.join(BUILTIN_MODELS)}")
    found = find_model(name) if target is None else None
    if found:
        return found
    d = Path(target) if target else model_dirs(name)[-1]
    d.mkdir(parents=True, exist_ok=True)
    spec = BUILTIN_MODELS[name]
    log(f"Downloading the built-in embedding model {name} (~{spec['size_mb']} MB, one time only)...")
    for fname, url in spec["files"].items():
        dest = d / fname
        if dest.is_file():
            continue
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
        except httpx.HTTPError as e:
            tmp.unlink(missing_ok=True)
            raise EmbeddingError(f"Could not download the embedding model ({e}). Check the internet connection, "
                                 f"or copy a 'models/{name}' folder next to the app.") from e
        tmp.replace(dest)
    log(f"Embedding model saved in {d}")
    return d


class BuiltinEmbedder(Embedder):
    """Runs a small embedding model inside RetroRAG (ONNX Runtime, CPU) - works anywhere, no AI server needed."""
    _loaded: dict = {}
    _lock = __import__("threading").Lock()

    def __init__(self, s: LibrarySettings):
        self.s = s
        self.name = resolve_builtin(s.embed_model)
        if self.name is None:
            raise EmbeddingError(f"'{s.embed_model}' isn't a built-in model (built-in: {', '.join(BUILTIN_MODELS)}). "
                                 "In Settings > Embeddings pick a 'Built-in' preset, or switch the provider back to "
                                 "the server that has this model.")
        self.spec = BUILTIN_MODELS[self.name]
        self.signature = f"builtin:{self.name}"

    def prepare(self, log=print):
        """Load (downloading first if needed). Called before bulk embedding so the download is logged."""
        return self._model(log)

    def _model(self, log=print):
        with self._lock:
            if self.name not in self._loaded:
                try:
                    import onnxruntime as ort
                    from tokenizers import Tokenizer
                except ImportError as e:
                    raise EmbeddingError("The built-in embedder needs:  pip install onnxruntime tokenizers") from e
                import os
                d = ensure_model(self.name, log=log)
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = max(1, (os.cpu_count() or 2))
                opts.log_severity_level = 3
                sess = ort.InferenceSession(str(d / "model.onnx"), sess_options=opts,
                                            providers=["CPUExecutionProvider"])
                tok = Tokenizer.from_file(str(d / "tokenizer.json"))
                tok.enable_truncation(max_length=self.spec["max_len"])
                pad_id = tok.token_to_id("[PAD]") or 0
                tok.enable_padding(pad_id=pad_id, pad_token="[PAD]")
                self._loaded[self.name] = (sess, tok, {i.name for i in sess.get_inputs()})
            return self._loaded[self.name]

    def embed(self, texts, is_query=False):
        sess, tok, inputs = self._model()
        pre = self.s.embed_query_prefix if is_query else self.s.embed_doc_prefix
        texts = [pre + t for t in texts]
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))   # similar lengths together = less padding
        out = [None] * len(texts)
        for k in range(0, len(order), 16):
            idx = order[k:k + 16]
            enc = tok.encode_batch([texts[i] for i in idx])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in inputs:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = sess.run(None, feeds)[0]
            if self.spec["pooling"] == "cls":
                vecs = hidden[:, 0, :]
            else:
                m = mask[..., None].astype(np.float32)
                vecs = (hidden * m).sum(1) / np.maximum(m.sum(1), 1)
                if self.spec["pooling"] == "mean_ln":     # nomic: layer-norm the pooled vector (no affine)
                    vecs = (vecs - vecs.mean(1, keepdims=True)) / np.sqrt(vecs.var(1, keepdims=True) + 1e-5)
            for j, i in enumerate(idx):
                out[i] = vecs[j]
        return self._norm(np.vstack(out))


def ollama_reachable(url: str = "http://localhost:11434") -> bool:
    try:
        return httpx.get(url.rstrip("/") + "/api/version", timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


def get_embedder(s: LibrarySettings) -> Embedder | None:
    p = (s.embed_provider or "none").lower()
    if p == "builtin":
        return BuiltinEmbedder(s)
    if p == "ollama":
        return OllamaEmbedder(s)
    if p == "openai":
        return OpenAIEmbedder(s)
    if p == "fastembed":
        return FastEmbedEmbedder(s)
    return None


def test_embedder(s: LibrarySettings) -> str:
    e = get_embedder(s)
    if e is None:
        return "Embeddings disabled: keyword search only."
    v = e.embed(["The SID chip is the MOS 6581."], is_query=False)
    return f"OK: {s.embed_provider} / {s.embed_model} returned {v.shape[1]}-dimension vectors."
