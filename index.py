"""Index a folder of documents into Neo4j, then build the retrieval index.

Run with:  uv run index.py <folder>

Picks up every .md and .txt file under <folder>, sends each through Perseus
build_graph (upload, job, poll), writes the resulting entities and relations
into Neo4j, then runs simple-graph-retriever's indexing pass so the MCP server
can answer queries.

This is the slow step - minutes, not seconds - which is exactly why it lives
here as a CLI and not behind an MCP tool call.

Re-running is cheap and safe:
  * Perseus reuses an already-succeeded job for an unchanged file, so nothing
    is re-processed or re-billed (pass --refresh to force new jobs).
  * The Neo4j load is additive and keyed on entity uuid via MERGE, so repeat
    runs do not duplicate entities. Files removed from the folder keep their
    nodes.

One thing is NOT additive: the final retrieval-index step always rebuilds from
scratch. simple-graph-retriever's index() recreates both Qdrant collections and
deletes every :Community and :GraphChunk node, plus all community_id
properties, before rebuilding them.
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    # Type-only: a runtime import here would load perseus_client before
    # load_dotenv() below has put the configuration into the environment.
    from perseus_client.models import KnowledgeGraph

ROOT = Path(__file__).parent

# Must happen before importing perseus_client or simple_graph_retriever: both
# read their settings from the environment at import time, and both look for a
# .env relative to the current working directory, which is not reliable here.
load_dotenv(ROOT / ".env")

SUFFIXES = {".md", ".txt"}

# Perseus ingests text, so PDFs are converted to Markdown first. Model taken
# from the perseus-client example examples/advanced/graph-rag-reporting-neo4j.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")

PDF_PROMPT = (
    "Convert this document to Markdown. Preserve headings, tables and lists, "
    "and keep figures as short descriptive text."
)


def convert_pdfs(folder: Path, recursive: bool, force: bool) -> list[Path]:
    """Convert each PDF under `folder` to Markdown beside it, using Gemini.

    The .md is written next to the .pdf so the normal scan picks it up, and so
    the conversion is visible and editable rather than hidden in a cache. A PDF
    is skipped when its .md already exists and is newer, which keeps re-runs
    free - conversion is the one step here that costs money per call rather
    than being reused like a Perseus job.
    """
    walker = folder.rglob("*") if recursive else folder.glob("*")
    pdfs = [p for p in sorted(walker) if p.is_file() and p.suffix.lower() == ".pdf"]
    if not pdfs:
        return []

    pending: list[tuple[Path, Path]] = []
    for pdf in pdfs:
        md = pdf.with_suffix(".md")
        if md.exists() and not force and md.stat().st_mtime >= pdf.stat().st_mtime:
            print(f"  cached:     {md.name}")
            continue
        pending.append((pdf, md))

    if not pending:
        return []

    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print(
            f"{len(pending)} PDF(s) need converting but GEMINI_API_KEY is not set.\n"
            "Set it in .env or your shell, or pass --no-pdf to ignore PDFs.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    from google import genai
    from google.genai import types as genai_types

    logging.getLogger("google_genai").setLevel(logging.ERROR)
    client = genai.Client()

    converted: list[Path] = []
    for pdf, md in pending:
        size_mb = pdf.stat().st_size / 1_000_000
        print(f"  converting: {pdf.name} ({size_mb:.1f} MB) via {GEMINI_MODEL}...")
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    genai_types.Part.from_bytes(
                        data=pdf.read_bytes(), mime_type="application/pdf"
                    ),
                    PDF_PROMPT,
                ],
                config=genai_types.GenerateContentConfig(
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=128)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop the rest
            print(f"    FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        if not response.text:
            print("    FAILED: the model returned no text", file=sys.stderr)
            continue
        md.write_text(response.text)
        print(f"    wrote {md.name} ({len(response.text):,} chars)")
        converted.append(md)

    return converted


def collect_files(folder: Path, recursive: bool) -> list[Path]:
    walker = folder.rglob("*") if recursive else folder.glob("*")
    found, skipped_empty = [], []
    for path in sorted(walker):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        if path.stat().st_size == 0:
            skipped_empty.append(path)
            continue
        found.append(path)
    for path in skipped_empty:
        print(f"  skip (empty): {path}")
    return found


def count(session, cypher: str) -> int:
    """Run a counting query and return its single `c` value.

    `Session.run(...).single()` is typed as returning `Record | None`, so this
    turns the None case into a clear error instead of a TypeError deep in a
    f-string.
    """
    record = session.run(cypher).single()
    if record is None:
        raise RuntimeError(f"expected one row from: {cypher}")
    return record["c"]


def neo4j_counts() -> dict[str, int]:
    """Node/relationship counts, used to verify the load actually landed."""
    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI", "neo4j://localhost:7687")
    auth = (
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()
        with driver.session() as session:
            return {
                "nodes": count(session, "MATCH (n) RETURN count(n) AS c"),
                "relationships": count(
                    session, "MATCH ()-[r]->() RETURN count(r) AS c"
                ),
                "communities": count(
                    session, "MATCH (c:Community) RETURN count(c) AS c"
                ),
                "chunks": count(session, "MATCH (c:GraphChunk) RETURN count(c) AS c"),
            }


def qdrant_counts() -> dict[str, object]:
    import requests

    url = os.environ.get("QDRANT_URL", "http://localhost:6333")
    out: dict[str, object] = {}
    for env, default in (
        ("QDRANT_CHUNKS_COLLECTION", "chunks"),
        ("QDRANT_COMMUNITIES_COLLECTION", "communities"),
        # New in simple-graph-retriever 1.0.0rc26: index() also runs
        # discover_schema(), which populates this collection.
        ("QDRANT_SCHEMA_COLLECTION", "schema"),
    ):
        name = os.environ.get(env, default)
        try:
            resp = requests.get(f"{url}/collections/{name}", timeout=5)
            resp.raise_for_status()
            out[name] = resp.json()["result"]["points_count"]
        except Exception as exc:  # noqa: BLE001
            out[name] = f"unavailable ({type(exc).__name__})"
    return out


def interlink_graphs(graphs: list["KnowledgeGraph"]) -> "KnowledgeGraph":
    """Merge the per-file graphs into one deduplicated graph, keyed on rdfs:label.

    Uses perseus_client.interlink(), which also re-attaches the services the
    merged graph needs for save_to_neo4j() and regenerates its ttl/cql content.

    Entities merge when they share a label *and* an entity type. Two entities
    with the same label under different types stay separate - the SDK's
    merge_different_entity_types defaults to False and the public API does not
    expose it. So an entity extracted as both :Platform and :SocialMediaPlatform
    remains two nodes, each holding its own share of the relationships.
    """
    import perseus_client

    before_entities = sum(len(g.entities) for g in graphs)
    before_relations = sum(len(g.relations) for g in graphs)

    merged = perseus_client.interlink(graphs)

    print(
        f"  entities  {before_entities} -> {len(merged.entities)} "
        f"({before_entities - len(merged.entities)} merged)"
    )
    print(
        f"  relations {before_relations} -> {len(merged.relations)} "
        f"({before_relations - len(merged.relations)} deduplicated)"
    )
    return merged


def wipe_graph() -> int:
    """Delete every node in Neo4j. Only called for --fresh."""
    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    auth = (
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    with GraphDatabase.driver(uri, auth=auth) as driver:
        with driver.session() as s:
            before = count(s, "MATCH (n) RETURN count(n) AS c")
            s.run("MATCH (n) DETACH DELETE n")
    return before


def write_manifest(files: list[Path], folder: Path) -> dict:
    """Summarise what got indexed, for the server to advertise in its tool description.

    Everything here is derived from what Perseus and the retriever already
    produced - the retriever writes a per-cluster summary to :Community.text -
    so there is no separate summarisation step or LLM call.

    A JSON file rather than Qdrant on purpose: the server reads this at startup
    to build its tool description, and must not depend on the stack being up
    just to register its tools.
    """
    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    auth = (
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    with GraphDatabase.driver(uri, auth=auth) as driver:
        with driver.session() as s:
            labels = [
                {"label": r["label"], "count": r["c"]}
                for r in s.run(
                    "MATCH (n) WHERE NOT n:GraphChunk AND NOT n:Community "
                    "UNWIND labels(n) AS label "
                    "RETURN label, count(*) AS c ORDER BY c DESC LIMIT 15"
                )
            ]
            # Exclude the retriever's own plumbing (:GraphChunk-[:CENTERED_ON]->)
            # so the advertised schema is domain relationships only.
            rel_types = [
                {"type": r["type"], "count": r["c"]}
                for r in s.run(
                    "MATCH (a)-[r]->(b) "
                    "WHERE NOT a:GraphChunk AND NOT b:GraphChunk "
                    "AND NOT a:Community AND NOT b:Community "
                    "RETURN type(r) AS type, count(*) AS c ORDER BY c DESC LIMIT 15"
                )
            ]
            # :Community.text is the summary the retriever already generated.
            topics = []
            for r in s.run(
                "MATCH (c:Community) WHERE c.text IS NOT NULL "
                "RETURN c.id AS id, c.size AS size, c.text AS text "
                "ORDER BY c.size DESC LIMIT 12"
            ):
                names = [
                    line.split(":", 1)[-1].strip()
                    for line in (r["text"] or "").splitlines()
                    if ":" in line
                    and not line.startswith(("Community ID", "Key Elements"))
                ]
                topics.append(
                    {"id": r["id"], "size": r["size"], "key_entities": names[:6]}
                )

    manifest = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_folder": str(folder),
        "documents": [
            {"name": str(p.relative_to(folder)), "bytes": p.stat().st_size}
            for p in files
        ],
        "entity_labels": labels,
        "relationship_types": rel_types,
        "topics": topics,
    }

    path = ROOT / "index_manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    print(f"wrote {path.name}: {len(labels)} labels, {len(topics)} topics")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("folder", type=Path, help="folder containing .md / .txt files")
    parser.add_argument(
        "--ontology", type=Path, default=None, help="optional ontology file (TTL/OWL)"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="force new Perseus jobs instead of reusing succeeded ones (may duplicate entities)",
    )
    parser.add_argument(
        "--no-recursive", action="store_true", help="do not descend into subfolders"
    )
    parser.add_argument(
        "--skip-retrieval-index",
        action="store_true",
        help="load Neo4j only; skip the community/chunk rebuild",
    )
    parser.add_argument(
        "--no-pdf",
        action="store_true",
        help="ignore PDFs instead of converting them to Markdown",
    )
    parser.add_argument(
        "--reconvert-pdf",
        action="store_true",
        help="re-convert PDFs even when an up-to-date .md already exists (costs a Gemini call each)",
    )
    parser.add_argument(
        "--convert-only",
        action="store_true",
        help="convert PDFs to Markdown and stop, without indexing",
    )
    parser.add_argument(
        "--no-interlink",
        action="store_true",
        help="do not deduplicate entities across/within documents before loading",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="DELETE every node in Neo4j before loading, for a clean rebuild "
        "(Perseus job reuse means this does not re-bill)",
    )
    args = parser.parse_args()

    # --convert-only never reaches Perseus, so it must not require that key.
    if not args.convert_only and not os.environ.get("PERSEUS_API_KEY"):
        print(
            "PERSEUS_API_KEY is not set. Copy sample.env to .env and fill it in.",
            file=sys.stderr,
        )
        return 2

    folder = args.folder.expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a directory: {folder}", file=sys.stderr)
        return 2

    # Converted before scanning, so the resulting .md files are picked up below.
    if not args.no_pdf:
        print(f"Looking for PDFs under {folder}...")
        convert_pdfs(folder, recursive=not args.no_recursive, force=args.reconvert_pdf)
        print()

    if args.convert_only:
        print("--convert-only: stopping before indexing.")
        return 0

    print(f"Scanning {folder} for {'/'.join(sorted(SUFFIXES))} files...")
    files = collect_files(folder, recursive=not args.no_recursive)
    if not files:
        print(
            f"No .md or .txt files found under {folder}. Nothing to index.",
            file=sys.stderr,
        )
        return 1
    for path in files:
        print(f"  found: {path.relative_to(folder)}")
    print(f"{len(files)} file(s) to index.\n")

    if args.fresh:
        removed = wipe_graph()
        print(f"--fresh: deleted {removed} existing node(s) from Neo4j\n")

    before = neo4j_counts()
    print(f"Neo4j before: {before['nodes']} nodes, {before['relationships']} rels\n")

    # Imported here, after load_dotenv, so the SDK sees the configuration.
    import perseus_client

    print(
        "Building graphs via Perseus (uploads, jobs, polling - this is the slow part)..."
    )
    results = perseus_client.build_graph(
        file_paths=[str(p) for p in files],
        ontology_path=str(args.ontology) if args.ontology else None,
        refresh_graph=args.refresh,
        # One bad file should not discard the work done for the others.
        return_exceptions=True,
    )

    graphs, failures = [], []
    for path, result in zip(files, results):
        if isinstance(result, BaseException):
            failures.append((path, result))
            print(f"  FAILED  {path.name}: {type(result).__name__}: {result}")
        else:
            graphs.append(result)
            print(
                f"  built   {path.name}: {len(result.entities)} entities, "
                f"{len(result.relations)} relations"
            )

    if not graphs:
        print("\nNo graphs were built; nothing to load.", file=sys.stderr)
        return 1

    if args.no_interlink:
        to_load = graphs
    else:
        print("\nDeduplicating entities (interlink on rdfs:label)...")
        to_load = [interlink_graphs(graphs)]

    expected_entities = sum(len(g.entities) for g in to_load)
    print(f"\nWriting {expected_entities} entities into Neo4j...")
    for graph in to_load:
        # Note: save_to_neo4j catches and logs every exception internally, so a
        # failure here is silent. The count check below is what actually tells
        # us whether the write landed.
        graph.save_to_neo4j()

    after = neo4j_counts()
    delta = after["nodes"] - before["nodes"]
    print(
        f"Neo4j after:  {after['nodes']} nodes (+{delta}), {after['relationships']} rels"
    )

    if delta == 0 and before["nodes"] == 0:
        print(
            "\nNothing was written to Neo4j even though graphs were built. "
            "save_to_neo4j() swallows its errors - check the log above for a "
            "Neo4j connection or Cypher failure.",
            file=sys.stderr,
        )
        return 1
    if delta == 0:
        print(
            "  (no new nodes - expected when re-indexing unchanged files, "
            "since the load MERGEs on entity uuid)"
        )

    if not args.skip_retrieval_index:
        from embedder import make_gemini_embed_fn
        from simple_graph_retriever.client import GraphRetrievalClient

        print("\nBuilding retrieval index (communities, chunks, embeddings)...")
        print("  note: this rebuilds the retrieval layer from scratch")
        client = GraphRetrievalClient(embed_fn=make_gemini_embed_fn())
        try:
            client.index()
        finally:
            client.close()

    print()
    write_manifest(files, folder)

    final = neo4j_counts()
    print("\n--- Summary ---")
    print(f"files indexed:   {len(files) - len(failures)}/{len(files)}")
    print(f"entities built:  {expected_entities}")
    print(f"neo4j nodes:     {final['nodes']} ({final['relationships']} relationships)")
    print(f"communities:     {final['communities']}")
    print(f"graph chunks:    {final['chunks']}")
    for name, count in qdrant_counts().items():
        print(f"qdrant {name}: {count}")

    if failures:
        print(f"\n{len(failures)} file(s) failed:", file=sys.stderr)
        for path, exc in failures:
            print(f"  {path}: {exc}", file=sys.stderr)
        return 1

    print("\nDone. The MCP server can now answer search_graph queries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
