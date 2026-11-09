"""Shared embed_fn / llm_fn for simple-graph-retriever.

As of 1.0.0rc26, the SDK no longer talks to any embedding or LLM provider
itself: GraphRetrievalClient / GraphIndexer / GraphRetriever require an
`embed_fn` callback from the caller - `Callable[[list[str]], list[list[float]]]`
- and NL2Cypher (query_natural_language / the use_nl2cypher retrieval step)
needs an `llm_fn` - `Callable[[str], str]` - on top of that. make_embed_fn()
wraps the local TEI (Text Embeddings Inference) service that docker-compose.yml
brings up, following the SDK's own examples/tei_embedder.py. make_llm_fn()
wraps Gemini, following the SDK README's own llm_fn example.

Used by both index.py (building the index) and server.py (embedding queries,
and - when a Gemini key is available - NL2Cypher), so this lives here rather
than in either.
"""

import logging
import os
from typing import Callable, List, Optional

import requests

logger = logging.getLogger(__name__)


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


def make_llm_fn(model: Optional[str] = None) -> Callable[[str], str]:
    """Build an llm_fn backed by Gemini, for simple-graph-retriever's NL2Cypher step.

    Only built when a Gemini key is set (see server.py's _get_client()) -
    genai.Client() picks GEMINI_API_KEY / GOOGLE_API_KEY up from the
    environment itself, same as index.py's PDF conversion.

    A failed call (bad/expired key, rate limit, network) returns "NONE" - the
    exact sentinel CypherGenerator's prompt tells the model to answer with
    when nothing in the schema is relevant - instead of raising. NL2Cypher's
    result is merged into retrieve_graph()'s subgraph, not a replacement for
    it, so one failed Gemini call should mean "no Cypher this time", not
    "search_graph fails" - the vector-search half of the pipeline would have
    worked fine on its own and shouldn't be held hostage by this addition.
    """
    from google import genai

    genai_model = model or os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")
    genai_client = genai.Client()

    def llm_fn(prompt: str) -> str:
        try:
            response = genai_client.models.generate_content(
                model=genai_model, contents=prompt
            )
            return response.text or "NONE"
        except Exception as exc:  # noqa: BLE001
            logger.warning("NL2Cypher: Gemini call failed, skipping: %s", exc)
            return "NONE"

    return llm_fn
