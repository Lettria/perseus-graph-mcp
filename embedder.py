"""Shared embed_fn for simple-graph-retriever.

As of 1.0.0rc26, the SDK no longer talks to any embedding provider itself:
GraphRetrievalClient / GraphIndexer / GraphRetriever all require an `embed_fn`
callback from the caller - `Callable[[list[str]], list[list[float]]]`. This
wraps the local TEI (Text Embeddings Inference) service that docker-compose.yml
brings up, following the SDK's own examples/tei_embedder.py.

Used by both index.py (building the index) and server.py (embedding queries),
so it lives here rather than in either.
"""

import os
from typing import Callable, List, Optional

import requests


def make_embed_fn(
    embedder_url: Optional[str] = None,
    batch_size: int = 32,
    max_chars: int = 500,
) -> Callable[[List[str]], List[List[float]]]:
    """Build an embed_fn backed by TEI.

    batch_size caps each request at TEI's default max-client-batch-size (32);
    sending more raises an HTTP 413. max_chars truncates each input - the SDK
    used to do this truncation itself before embed_fn became caller-supplied,
    so this preserves that behavior rather than sending TEI arbitrarily long
    text.

    A request failure (TEI down, non-200 response, ...) raises via
    raise_for_status() rather than returning an empty vector. That is
    deliberate: server.py's search_graph already wraps retrieve_graph() in a
    try/except that turns any exception into a clear ToolError, which is more
    useful than the SDK silently treating a down embedder the same as "no
    results".
    """
    url = embedder_url or os.environ.get("EMBEDDER_URL", "http://localhost:8080")

    def embed_fn(texts: List[str]) -> List[List[float]]:
        truncated = [t[:max_chars] for t in texts]
        embeddings: List[List[float]] = []
        for i in range(0, len(truncated), batch_size):
            chunk = truncated[i : i + batch_size]
            resp = requests.post(url, json={"inputs": chunk}, timeout=30)
            resp.raise_for_status()
            embeddings.extend(resp.json())
        return embeddings

    return embed_fn
