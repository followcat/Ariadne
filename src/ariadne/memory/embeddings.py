from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from ..errors import AriadneError, app_error


class EmbeddingProvider(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass
class HashEmbeddingProvider:
    """Deterministic local embedding (no network) for tests and offline semantic.

    Not a substitute for real model embeddings, but stable and dependency-free.
    """

    dims: int = 64

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dims
            tokens = (text or "").lower().split()
            if not tokens:
                out.append(vec)
                continue
            for tok in tokens:
                h = hashlib.sha256(tok.encode("utf-8")).digest()
                for i in range(self.dims):
                    vec[i] += (h[i % len(h)] - 128) / 128.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


@dataclass
class OpenAIEmbeddingProvider:
    base_url: str
    api_key: str
    model: str = "text-embedding-3-small"
    timeout: float = 60.0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        base = self.base_url.rstrip("/")
        url = base + ("/embeddings" if base.endswith("/v1") else "/v1/embeddings")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "ariadne/0.2.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                obj = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise AriadneError(
                app_error("ARIADNE_MODEL_ERROR", f"embedding failed: {type(exc).__name__}: {exc}")
            ) from exc
        data = obj.get("data") or []
        # sort by index if present
        data = sorted(data, key=lambda x: int(x.get("index") or 0))
        return [list(map(float, item.get("embedding") or [])) for item in data]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
