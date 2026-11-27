"""Model-agnostic embedding providers.

Verification note (CP-W03): the AGCore AI Gateway client (`app/gateway/client.py`) exposes chat
completion routes only — there is **no** embedding endpoint to reuse. The gateway adapter below
therefore reports itself unavailable instead of pretending to work, and the engine runs either
the offline local provider or a configured remote embedding API.

Providers are never mixed: the vector index key is `{model_id}:{dimension}`, so a model or
dimension change starts a separate index instead of reusing incompatible vectors.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
from typing import Any, List, Optional, Protocol, Sequence

import httpx

from app.workspace.config import WorkspaceSettings

_HANGUL = r"\uac00-\ud7a3"
_TOKEN_RE = re.compile(rf"[0-9A-Za-z{_HANGUL}]+(?:[._-][0-9A-Za-z{_HANGUL}]+)*")
GATEWAY_EMBEDDING_ENDPOINT_VERIFIED = False


class EmbeddingUnavailable(RuntimeError):
    """The provider cannot serve requests right now (network, config or quota)."""


class EmbeddingProvider(Protocol):
    provider_name: str
    model_id: str
    dimension: int
    batch_size: int
    max_input_tokens: int
    timeout: float
    available: bool
    index_key: str

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]: ...

    def embed_query(self, text: str) -> List[float]: ...


def _l2(vector: List[float]) -> List[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


class LocalHashEmbeddingProvider:
    """Offline, dependency-free multilingual signed feature hashing.

    Deterministic across processes and machines: word features plus character 2/3-grams (which
    capture Korean particles and technical stems). Quality is below a trained model but it needs
    no GPU, no network and no paid API, which is the preview baseline.
    """

    provider_name = "local"

    def __init__(self, settings: WorkspaceSettings) -> None:
        self.model_id = settings.embedding_model if settings.embedding_provider == "local" else "local-hash-v1"
        self.dimension = int(settings.embedding_dimension)
        self.batch_size = max(1, int(settings.embedding_batch_size))
        self.max_input_tokens = int(settings.embedding_max_input_tokens)
        self.timeout = float(settings.embedding_timeout_seconds)
        self.available = True
        self.index_key = f"{self.model_id}:{self.dimension}"

    def _embed_one(self, text: str) -> List[float]:
        vector = [0.0] * self.dimension
        cleaned = re.sub(r"\s+", " ", (text or "").casefold()).strip()
        if not cleaned:
            return vector
        for token in _TOKEN_RE.findall(cleaned):
            digest = hashlib.blake2b(f"w:{token}".encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimension
            sign = 1.0 if (digest[4] & 1) == 0 else -1.0
            vector[index] += sign * 2.0
            if len(token) >= 2:
                for size in (2, 3):
                    if len(token) < size:
                        continue
                    for start in range(len(token) - size + 1):
                        gram = token[start : start + size]
                        gram_digest = hashlib.blake2b(f"g{size}:{gram}".encode("utf-8"), digest_size=8).digest()
                        gram_index = int.from_bytes(gram_digest[:4], "little") % self.dimension
                        gram_sign = 1.0 if (gram_digest[4] & 1) == 0 else -1.0
                        vector[gram_index] += gram_sign
        return _l2(vector)

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return [self._embed_one(text) for text in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._embed_one(text)


class RemoteEmbeddingProvider:
    """OpenAI-compatible (`/v1/embeddings`) or Ollama (`/api/embed`) HTTP embedding provider."""

    provider_name = "remote"

    def __init__(
        self,
        settings: WorkspaceSettings,
        *,
        client: Optional[httpx.Client] = None,
        probe: bool = False,
    ) -> None:
        self.url = (settings.embedding_url or "").strip()
        self.model_id = settings.embedding_remote_model
        self.dimension = int(settings.embedding_dimension)
        self.batch_size = max(1, int(settings.embedding_batch_size))
        self.max_input_tokens = int(settings.embedding_max_input_tokens)
        self.timeout = float(settings.embedding_timeout_seconds)
        self.api_key = settings.embedding_api_key
        self.index_key = f"{self.model_id}:{self.dimension}"
        self._client = client
        self._lock = threading.Lock()
        self._probed = False
        self._available = bool(self.url)
        if probe and self.url:
            self.available  # probe on construction when explicitly requested

    @property
    def available(self) -> bool:
        if not self.url:
            return False
        if self._probed:
            return self._available
        with self._lock:
            if not self._probed:
                try:
                    self.embed_documents(["ping"])
                    self._available = True
                except Exception:
                    self._available = False
                self._probed = True
        return self._available

    def _client_or_new(self) -> tuple[httpx.Client, bool]:
        if self._client is not None:
            return self._client, False
        return httpx.Client(timeout=self.timeout, trust_env=False), True

    def _embed_batch(self, client: httpx.Client, texts: Sequence[str]) -> List[List[float]]:
        payload: dict[str, Any] = {
            "model": self.model_id,
            "input": [text[: self.max_input_tokens * 4] for text in texts],
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        is_ollama = self.url.endswith("/api/embed")
        if is_ollama:
            payload.pop("input", None)
            payload["input"] = [text[: self.max_input_tokens * 4] for text in texts]
        response = client.post(self.url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        if is_ollama:
            return [list(map(float, vector)) for vector in data.get("embeddings") or []]
        items = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        return [list(map(float, item["embedding"])) for item in items]

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        if not self.url:
            raise EmbeddingUnavailable("embedding_url_not_configured")
        client, owned = self._client_or_new()
        vectors: List[List[float]] = []
        try:
            for start in range(0, len(texts), self.batch_size):
                batch = list(texts[start : start + self.batch_size])
                if not batch:
                    continue
                try:
                    vectors.extend(self._embed_batch(client, batch))
                except (httpx.HTTPError, ValueError, KeyError) as exc:
                    self._available = False
                    self._probed = True
                    raise EmbeddingUnavailable(str(exc)) from exc
        finally:
            if owned:
                client.close()
        if len(vectors) != len(texts):
            raise EmbeddingUnavailable("embedding_count_mismatch")
        if vectors:
            observed = len(vectors[0])
            if observed != self.dimension:
                # Dimension is a property of the model, not a per-request choice.
                self.dimension = observed
                self.index_key = f"{self.model_id}:{observed}"
        return [_l2(vector) for vector in vectors]

    def embed_query(self, text: str) -> List[float]:
        vectors = self.embed_documents([text])
        if not vectors:
            raise EmbeddingUnavailable("empty_embedding")
        return vectors[0]


class GatewayEmbeddingProvider:
    """Placeholder adapter for a future AI Gateway embedding route.

    The gateway has no embedding endpoint today (`GATEWAY_EMBEDDING_ENDPOINT_VERIFIED = False`),
    so this provider fails closed instead of silently degrading quality.
    """

    provider_name = "gateway"
    model_id = "gateway-embedding"
    dimension = 0
    batch_size = 1
    max_input_tokens = 0
    timeout = 0.0
    available = False
    index_key = "gateway-embedding:0"

    def __init__(self, settings: WorkspaceSettings) -> None:
        self.settings = settings

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        raise EmbeddingUnavailable("gateway_embedding_endpoint_not_available")

    def embed_query(self, text: str) -> List[float]:
        raise EmbeddingUnavailable("gateway_embedding_endpoint_not_available")


def build_embedder(settings: WorkspaceSettings) -> EmbeddingProvider:
    mode = (settings.embedding_provider or "auto").strip().lower()
    if mode == "local":
        return LocalHashEmbeddingProvider(settings)
    if mode in {"remote", "openai", "ollama", "http"}:
        return RemoteEmbeddingProvider(settings)
    if mode == "gateway":
        return GatewayEmbeddingProvider(settings)
    # auto: a configured remote endpoint wins, otherwise the offline local provider
    if (settings.embedding_url or "").strip():
        return RemoteEmbeddingProvider(settings)
    return LocalHashEmbeddingProvider(settings)
