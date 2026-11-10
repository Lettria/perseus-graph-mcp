"""Shared embed_fn / llm_fn for simple-graph-retriever.

As of 1.0.0rc26, the SDK no longer talks to any embedding or LLM provider
itself: GraphRetrievalClient / GraphIndexer / GraphRetriever require an
`embed_fn` callback from the caller - `Callable[[list[str]], list[list[float]]]`
- and NL2Cypher (query_natural_language / the use_nl2cypher retrieval step)
needs an `llm_fn` - `Callable[[str], str]` - on top of that. make_gemini_embed_fn()
wraps Gemini's embedding API for the former; make_llm_fn() wraps Gemini's
generation API for the latter, following the SDK README's own llm_fn example.

Used by both index.py (building the index) and server.py (embedding queries,
and - when a Gemini key is available - NL2Cypher), so this lives here rather
than in either.
"""

import logging
import os
from typing import Callable, List, Optional, cast

from google import genai
from google.genai import types as genai_types
from perseus_client.exceptions import PerseusException

logger = logging.getLogger(__name__)


def make_gemini_embed_fn(
    genai_client: Optional[genai.Client] = None,
    model: str = "gemini-embedding-001",
    batch_size: int = 100,
    max_chars: int = 6000,
) -> Callable[[List[str]], List[List[float]]]:
    """
    simple_graph_retriever no longer talks to any embedder itself; this
    wires its required embed_fn callback to Gemini's embedding API instead
    of running a local TEI container. Swap this out for Azure OpenAI,
    OpenAI, etc. in your own code.

    batch_size caps how many texts go into a single embed_content call,
    staying under the Gemini API's per-request batch limit - confirmed
    against the real API that a 100-item batch embeds cleanly.

    max_chars truncates each text before sending it. gemini-embedding-001
    caps input at 2,048 tokens per text and - confirmed empirically, not
    from docs - does not error past that: it silently truncates, so
    anything beyond the cutoff is invisible to retrieval with no signal
    anywhere that it happened (a marker placed at the end of an oversized
    text produced an embedding identical to the same text with no marker
    at all). 6000 chars leaves real margin under that limit at ~4
    chars/token for English, and makes the cut ours - deliberate and
    logged - rather than Gemini's silent one.
    """
    client = genai_client or genai.Client()

    def embed_fn(texts: List[str]) -> List[List[float]]:
        truncated = []
        for text in texts:
            if len(text) > max_chars:
                logger.warning(
                    "embed_fn: truncating a %d-char text to %d chars before "
                    "sending it to Gemini (gemini-embedding-001's input limit "
                    "would otherwise silently drop the tail)",
                    len(text),
                    max_chars,
                )
                text = text[:max_chars]
            truncated.append(text)

        embeddings: List[List[float]] = []
        for i in range(0, len(truncated), batch_size):
            chunk = truncated[i : i + batch_size]
            response = client.models.embed_content(
                model=model, contents=cast(genai_types.ContentListUnion, chunk)
            )
            for embedding in response.embeddings or []:
                if embedding.values is None:
                    raise PerseusException(
                        f"Gemini returned no embedding values for model {model!r}."
                    )
                embeddings.append(embedding.values)
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
