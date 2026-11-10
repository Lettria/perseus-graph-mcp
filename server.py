"""The perseus-graph-mcp server: read-only search over an indexed graph.

Indexing happens offline in index.py. This server only queries what is already
in Neo4j and Qdrant, so every tool call returns in about a second.
"""

import contextlib
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent

# Before importing simple_graph_retriever: it reads settings from the
# environment at import time, and looks for a .env relative to the current
# working directory - which Claude Desktop sets to somewhere arbitrary.
load_dotenv(ROOT / ".env")

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402

# Logs must go to stderr: stdout carries the JSON-RPC protocol, and anything else
# written there corrupts it. Claude Desktop captures stderr into
# ~/.config/Claude/logs/mcp-server-perseus-graph-mcp.log
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("perseus-graph-mcp")

_client = None

# A tool description sits in the prompt prefix for every request in a
# conversation, unlike a result which is paid once. So the manifest is
# summarised down to a budget rather than enumerated.
_DESCRIPTION_BUDGET = 1200

BASE_DESCRIPTION = """Search the user's own indexed knowledge graph, built from their documents.

USE THIS FIRST, before answering from your own knowledge, whenever the question
touches any of the subjects or entity types listed at the end of this
description. Those documents are the user's authoritative source: they are more
specific, more current, and more trustworthy for these topics than your
training data. Answering from memory instead will give the user a generic or
out-of-date answer about their own material.

If a search comes back empty, say so and then fall back to general knowledge -
do not silently substitute one for the other.

ONE SUBJECT PER CALL. The query is embedded as a single vector, so a question
spanning two unrelated subjects produces a blended vector that matches neither
of them well, and the max_chunks / max_communities budget is shared across the
whole query - it gets spent on whichever subject scores higher, and the other
drops out silently. Two focused searches beat one broad one.

So decompose a multi-subject question and issue one call per subject; several
calls in the same turn are expected and can be made in parallel. Then combine
the results yourself.

  "How does X compare to Y?"        -> two calls: "X", then "Y"
  "What do X and Y have in common?" -> two calls, then intersect
  "Tell me about X and also Z"      -> two calls

Do not over-split, though. One subject together with its relationships is
still ONE call - "what does X integrate with?" or "who are X's customers?" are
single subjects, and the graph already returns each matched entity with its
relationships. To reach further around a subject, raise max_hops rather than
issuing more queries. Short, specific, single-subject queries work best;
multi-clause sentences dilute the match.

Returns Markdown: one section per entity, listing its properties and its
outgoing relationships.

Args:
    query: A natural-language question, e.g. "what causes delivery delays?".
    max_hops: How far to expand from each matched entity. 1 keeps results
        tight; 2 or 3 pull in more context and much more text.
    max_chunks: Maximum number of entity-level matches to seed from.
    max_communities: Maximum number of topic clusters to consider.
    allowed_rel_types: If given, keep only these relationship types.
    denied_rel_types: If given, drop these relationship types."""


def _describe_index() -> str:
    """Summarise what is actually indexed, so the model knows when this tool helps.

    Read from index_manifest.json, written by index.py. Deliberately a file and
    not a database query: this runs at import time to build the tool
    description, and must not fail or hang when the stack is down.
    """
    path = ROOT / "index_manifest.json"
    if not path.exists():
        return (
            "\n\nNOTE: nothing has been indexed yet, so this tool cannot answer "
            "anything until `index.py` has been run."
        )

    try:
        m = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 - a bad manifest must not break startup
        log.warning("ignoring unreadable index_manifest.json: %s", exc)
        return ""

    docs = m.get("documents", [])
    labels = [entry["label"] for entry in m.get("entity_labels", [])][:12]
    rels = [entry["type"] for entry in m.get("relationship_types", [])][:12]

    # Entity names from the largest clusters: the most direct signal of whether
    # a given question is answerable from this graph at all.
    subjects: list[str] = []
    for topic in m.get("topics", []):
        for name in topic.get("key_entities", []):
            if name and name not in subjects:
                subjects.append(name)

    parts = []
    if docs:
        names = ", ".join(d["name"] for d in docs[:10])
        more = f", +{len(docs) - 10} more" if len(docs) > 10 else ""
        sources = ", ".join(m.get("sources", [])) or "an indexed path"
        parts.append(
            f"\n\nThis graph was built from {len(docs)} document(s) in "
            f"{sources}: {names}{more}."
        )
    else:
        parts.append(f"\n\nIndexed {m.get('generated_at', 'at an unknown date')}.")
    if subjects:
        parts.append(
            "Call this tool for any question mentioning these subjects: "
            f"{', '.join(subjects[:25])}."
        )
    if labels:
        parts.append(f"Entity types in the graph: {', '.join(labels)}.")
    if rels:
        parts.append(
            f"Relationship types (use with allowed_rel_types): {', '.join(rels)}."
        )

    text = "\n".join(parts)
    if len(text) > _DESCRIPTION_BUDGET:
        text = text[:_DESCRIPTION_BUDGET].rsplit(" ", 1)[0] + " ..."
    return text


def _describe_schema() -> str:
    """Summarise the graph's real labels/relationship types, for run_cypher's description.

    Reused from index_manifest.json rather than queried fresh: this runs at
    import time to build the tool description, same constraint as
    _describe_index(), and the entity_labels/relationship_types index.py
    already wrote are exactly what a model needs to write correct Cypher
    against this specific graph instead of guessing from generic training
    data.
    """
    path = ROOT / "index_manifest.json"
    if not path.exists():
        return (
            "\n\nNOTE: nothing has been indexed yet, so there is no schema to "
            "query until `index.py` has been run."
        )
    try:
        m = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 - a bad manifest must not break startup
        log.warning("ignoring unreadable index_manifest.json: %s", exc)
        return ""

    labels = [entry["label"] for entry in m.get("entity_labels", [])][:15]
    rels = [entry["type"] for entry in m.get("relationship_types", [])][:15]
    parts = []
    if labels:
        parts.append(f"\n\nNode labels in the graph: {', '.join(labels)}.")
    if rels:
        parts.append(f"Relationship types: {', '.join(rels)}.")
    return "\n".join(parts)


CYPHER_DESCRIPTION = """Run a read-only Cypher query directly against the user's Neo4j graph.

Use this for what search_graph's vector search structurally cannot do:
exact counts ("how many X are there"), aggregates, exhaustive lists ("every
Y connected to Z"), or precise multi-hop structural patterns you can name
directly in Cypher. Prefer search_graph for anything that's really a
semantic question - "what does X do", "tell me about Y" - raw Cypher only
helps once you already know which labels/relationships to ask for.

READ-ONLY, enforced by Neo4j itself: the session this runs in is opened in
read access mode, so any CREATE/MERGE/DELETE/SET/REMOVE is rejected by the
server before it can touch the graph, not filtered client-side. There is no
way to use this tool to modify data.

Every query has a 10-second server-side timeout and is capped at max_rows
results (extra rows are dropped, not an error) - write your own LIMIT for an
exploratory query rather than relying on max_rows, since the timeout bounds
compute, not row count: an unindexed MATCH over the whole graph can time out
long before max_rows would have capped it.

Args:
    query: A Cypher query. Node labels and relationship types must match the
        graph's real schema below - invented ones simply match nothing.
    parameters: Optional Cypher query parameters (referenced as $name in the
        query), e.g. {"name": "Ada Lovelace"}. Prefer this over string-building
        values into the query text.
    max_rows: Maximum rows to return. Lower this for a quick existence check;
        raise it for a listing you expect to be long."""


_cypher_driver = None


def _get_cypher_driver():
    """Build a dedicated Neo4j driver for run_cypher, then reuse it.

    Deliberately separate from _get_client()'s GraphRetrievalClient: that
    client's own Neo4jAdapter opens plain read+write sessions internally
    (confirmed by reading its source), so reusing it here would not give
    run_cypher the read-only guarantee its whole design rests on. This
    driver is only ever used to open read-access sessions - see run_cypher.
    """
    global _cypher_driver
    if _cypher_driver is None:
        from neo4j import GraphDatabase

        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        auth = (
            os.environ.get("NEO4J_USER", "neo4j"),
            os.environ.get("NEO4J_PASSWORD", "password"),
        )
        try:
            _cypher_driver = GraphDatabase.driver(uri, auth=auth)
            _cypher_driver.verify_connectivity()
        except Exception as exc:
            raise ToolError(
                f"Cannot connect to Neo4j ({type(exc).__name__}: {exc}). "
                f"Is the stack running? Start it with: uv run start.py"
            ) from exc
        log.info("connected to Neo4j for run_cypher")
    return _cypher_driver


def _get_client():
    """Build the retrieval client on first use, then reuse it.

    Deliberately lazy: Neo4jAdapter.__init__ calls verify_connectivity(), so
    building this at import time would kill the server at startup whenever the
    Docker stack is down.
    """
    global _client
    if _client is None:
        from embedder import make_gemini_embed_fn
        from simple_graph_retriever.client import GraphRetrievalClient

        llm_fn = None
        if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
            from embedder import make_llm_fn

            llm_fn = make_llm_fn()
            log.info("Gemini key found: NL2Cypher enabled")
        else:
            log.info("no Gemini key: NL2Cypher disabled")

        try:
            _client = GraphRetrievalClient(
                embed_fn=make_gemini_embed_fn(), llm_fn=llm_fn
            )
        except Exception as exc:
            raise ToolError(
                f"Cannot connect to the graph database ({type(exc).__name__}: {exc}). "
                f"Is the stack running? Start it with: uv run start.py"
            ) from exc
        log.info("connected to graph database")
    return _client


def _diagnose_empty() -> str:
    """Explain an empty result.

    The retriever returns None or an empty result for three very different
    reasons and reports none of them: search_qdrant() swallows a missing
    collection into []. (embed_fn, the one from this project's embedder.py,
    raises rather than swallowing - a down embedder is caught earlier, by
    search_graph's own try/except around retrieve_graph(). This function
    covers what is left: a genuinely missing or empty index.) So probe the
    dependencies directly rather than guessing.
    """
    import requests

    embedder = os.environ.get("EMBEDDER_URL", "http://localhost:8080")
    try:
        resp = requests.post(embedder, json={"inputs": "ping"}, timeout=10)
        resp.raise_for_status()
        resp.json()[0]
    except Exception as exc:  # noqa: BLE001
        return (
            f"The embedding service at {embedder} is not answering "
            f"({type(exc).__name__}). Without it no query can be embedded. "
            f"Start the stack with: uv run start.py"
        )

    qdrant = os.environ.get("QDRANT_URL", "http://localhost:6333")
    chunks = os.environ.get("QDRANT_CHUNKS_COLLECTION", "chunks")
    try:
        resp = requests.get(f"{qdrant}/collections/{chunks}", timeout=5)
        if resp.status_code == 404:
            return (
                "Nothing has been indexed yet: the Qdrant collection "
                f"'{chunks}' does not exist. Run: uv run index.py <folder>"
            )
        resp.raise_for_status()
        if resp.json()["result"]["points_count"] == 0:
            return (
                f"The '{chunks}' collection is empty, so there is nothing to "
                f"search. Run: uv run index.py <folder>"
            )
    except Exception as exc:  # noqa: BLE001
        return (
            f"Cannot reach the vector database at {qdrant} "
            f"({type(exc).__name__}). Start the stack with: uv run start.py"
        )

    return "No part of the indexed graph matched this query."


def _server_instructions() -> str:
    """Server-level guidance, sent to the client in the initialize response.

    A second, independent channel from the tool description: some clients show
    this to the model as standing context about the server as a whole.
    """
    base = (
        "This server exposes the user's own knowledge graph, built from their "
        "documents with Lettria Perseus. For any question about the subjects it "
        "covers, query it with search_graph before answering from memory - it is "
        "the authoritative source for the user's material, and a generic answer "
        "from training data is usually the wrong one here. Retrieval is "
        "single-subject: break a question covering several subjects into one "
        "search_graph call per subject rather than one combined query."
    )
    path = ROOT / "index_manifest.json"
    if not path.exists():
        return base
    try:
        m = json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return base
    subjects = [
        name for topic in m.get("topics", []) for name in topic.get("key_entities", [])
    ]
    if not subjects:
        return base
    unique = list(dict.fromkeys(subjects))[:15]
    return f"{base}\n\nThe graph covers, among other things: {', '.join(unique)}."


mcp = MCPServer(
    "perseus-graph-mcp",
    version="0.1.0",
    instructions=_server_instructions(),
)


@mcp.tool(
    description=BASE_DESCRIPTION + _describe_index(),
    # Declared field names are snake_case; they serialise to the camelCase
    # wire form (readOnlyHint / openWorldHint) via their aliases.
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    # Without this the SDK auto-generates an output schema from the "-> str"
    # return type and wraps every result in structuredContent={"result": ...}
    # alongside the plain content block. Claude Desktop's result viewer shows
    # that structuredContent JSON instead of rendering the markdown in
    # content - this is a plain-text tool, there's no structured shape to
    # preserve, so turn the wrapper off entirely.
    structured_output=False,
)
def search_graph(
    query: str,
    max_hops: int = 1,
    max_chunks: int = 10,
    max_communities: int = 3,
    allowed_rel_types: list[str] | None = None,
    denied_rel_types: list[str] | None = None,
) -> str:
    from simple_graph_retriever.models import RetrievalConfig

    log.info("search_graph(query=%r, max_hops=%d)", query, max_hops)

    client = _get_client()
    config = RetrievalConfig(
        max_hops=max_hops,
        max_chunks=max_chunks,
        max_communities=max_communities,
        allowed_rel_types=allowed_rel_types,
        denied_rel_types=denied_rel_types,
        # Default True upstream, but it needs an llm_fn on the client to do
        # anything - without one it just logs a warning on every call. Gated
        # on whether _get_client() actually built one (it does when a Gemini
        # key is present; see embedder.make_llm_fn).
        use_nl2cypher=client.llm_fn is not None,
    )

    try:
        # Defensive: simple-graph-retriever's fetch_subgraph() (fixed in rc26)
        # and indexer.py (fixed in rc27) both used to have a bare print() -
        # neither does anymore, but stdout is the JSON-RPC channel on a stdio
        # server, so this stays as cheap insurance against a future one
        # landing on this call path again.
        with contextlib.redirect_stdout(sys.stderr):
            result = client.retrieve_graph(query, config)
    except Exception as exc:
        log.exception("retrieval failed")
        raise ToolError(f"Retrieval failed: {type(exc).__name__}: {exc}") from exc

    if result is None or not result.nodes:
        reason = _diagnose_empty()
        log.info("empty result: %s", reason)
        return reason

    log.info(
        "returning %d nodes, %d relationships",
        len(result.nodes),
        len(result.relationships),
    )
    return result.to_markdown()


@mcp.tool(
    description=CYPHER_DESCRIPTION + _describe_schema(),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
    structured_output=False,
)
def run_cypher(
    query: str,
    parameters: dict[str, object] | None = None,
    max_rows: int = 100,
) -> str:
    from itertools import islice
    from typing import LiteralString, cast

    from neo4j import READ_ACCESS, Query
    from neo4j.exceptions import ClientError

    log.info("run_cypher(query=%r, max_rows=%d)", query, max_rows)

    driver = _get_cypher_driver()
    # 10s is fixed, not a tool argument: it is the safety backstop, not a
    # knob for the model to raise away. See CYPHER_DESCRIPTION for why this
    # bounds compute while max_rows only bounds what comes back.
    #
    # Query.text is typed LiteralString - the driver's own nudge against
    # building Cypher by interpolating untrusted values into query text. That
    # is not what's happening here: query is this tool's entire point (an
    # arbitrary, caller-supplied Cypher string), and untrusted values belong
    # in `parameters` ($name placeholders), never string-built into `query`
    # itself - exactly the separation the LiteralString constraint exists to
    # encourage. The cast tells the type checker that, it doesn't bypass it.
    cypher = Query(cast(LiteralString, query), timeout=10.0)

    try:
        with driver.session(default_access_mode=READ_ACCESS) as session:
            # Defensive, same reasoning as search_graph's redirect: stdout is
            # the JSON-RPC channel on a stdio server, and nothing here is
            # known to print, but the cost of guarding against it is zero.
            with contextlib.redirect_stdout(sys.stderr):
                result = session.run(cypher, parameters or {})
                rows = [r.data() for r in islice(result, max_rows + 1)]
    except ClientError as exc:
        if exc.code == "Neo.ClientError.Statement.AccessMode":
            raise ToolError(
                "This query would write to the graph. run_cypher is read-only "
                "by design - Neo4j itself rejected the write, not a client-side "
                "filter. Use index.py / clear.py to change the graph's data."
            ) from exc
        if (
            exc.code
            == "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"
        ):
            raise ToolError(
                "This query exceeded its 10-second time limit and was cancelled "
                "by Neo4j before producing any rows. Narrow the MATCH (filter on "
                "a label or indexed property before expanding) or add your own "
                "LIMIT rather than relying on max_rows, which only caps what's "
                "returned, not how much work the query does."
            ) from exc
        log.exception("run_cypher failed")
        raise ToolError(f"Cypher query failed: {exc.code}: {exc.message}") from exc
    except Exception as exc:
        log.exception("run_cypher failed")
        raise ToolError(f"Cypher query failed: {type(exc).__name__}: {exc}") from exc

    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]
    log.info("run_cypher returning %d row(s), truncated=%s", len(rows), truncated)

    if not rows:
        return "Query succeeded with no rows."

    body = json.dumps(rows, indent=2, default=str)
    if truncated:
        body += f"\n\n(truncated at max_rows={max_rows}; more rows were available)"
    return body


if __name__ == "__main__":
    log.info("perseus-graph-mcp server starting on stdio (pid %d)", os.getpid())
    try:
        mcp.run()  # stdio transport, which is what Claude Desktop speaks
    finally:
        log.info("perseus-graph-mcp server stopped")
