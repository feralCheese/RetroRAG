"""Embedding providers. All return L2-normalised float32 numpy arrays.

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


def get_embedder(s: LibrarySettings) -> Embedder | None:
    p = (s.embed_provider or "none").lower()
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
