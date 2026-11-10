"""Delete everything from Neo4j and Qdrant - the local databases, not Perseus.

Run with:  uv run clear.py

Deletes every node from Neo4j and the chunks/communities/schema collections
from Qdrant, then removes index_manifest.json so the server stops
advertising a corpus that no longer exists.

The underlying documents and Perseus's own job history are untouched - that
cache lives server-side, not in these local databases - so a re-index
afterward (`uv run index.py <path>...`) rebuilds everything without
re-processing or re-billing. There is no undo for the local databases
themselves, though: once this runs, the graph is gone until you re-index.
"""

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def clear_neo4j() -> int:
    """Delete every node (and its relationships). Returns how many there were."""
    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    auth = (
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()
        with driver.session() as session:
            record = session.run("MATCH (n) RETURN count(n) AS c").single()
            before = record["c"] if record else 0
            session.run("MATCH (n) DETACH DELETE n")
    return before


def clear_qdrant() -> list[str]:
    """Delete the chunks/communities/schema collections. Returns which existed.

    DELETE /collections/{name} returns HTTP 200 with result:false (not a 404)
    when the collection doesn't exist - confirmed directly against a running
    instance rather than assumed - so that's the signal used to tell "existed
    and was deleted" apart from "was already gone".
    """
    url = os.environ.get("QDRANT_URL", "http://localhost:6333")
    deleted = []
    for env, default in (
        ("QDRANT_CHUNKS_COLLECTION", "chunks"),
        ("QDRANT_COMMUNITIES_COLLECTION", "communities"),
        ("QDRANT_SCHEMA_COLLECTION", "schema"),
    ):
        name = os.environ.get(env, default)
        resp = requests.delete(f"{url}/collections/{name}", timeout=10)
        resp.raise_for_status()
        if resp.json().get("result"):
            deleted.append(name)
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--neo4j-only",
        action="store_true",
        help="only clear Neo4j, leave the Qdrant collections alone",
    )
    parser.add_argument(
        "--qdrant-only",
        action="store_true",
        help="only clear Qdrant, leave Neo4j alone",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation prompt"
    )
    args = parser.parse_args()

    if args.neo4j_only and args.qdrant_only:
        print("--neo4j-only and --qdrant-only are mutually exclusive.", file=sys.stderr)
        return 2

    targets = []
    if not args.qdrant_only:
        targets.append("every node in Neo4j")
    if not args.neo4j_only:
        targets.append("the chunks/communities/schema collections in Qdrant")
    print("This will permanently delete " + " and ".join(targets) + ".")
    print(
        "The source documents are untouched, and a re-index afterward reuses Perseus's "
        "already-succeeded jobs rather than re-processing or re-billing - but there is "
        "no undo for what's about to be deleted here."
    )

    if not args.yes:
        reply = input("\nType 'yes' to continue: ").strip().lower()
        if reply != "yes":
            print("Aborted - nothing was deleted.")
            return 1
        print()

    if not args.qdrant_only:
        try:
            removed = clear_neo4j()
        except Exception as exc:  # noqa: BLE001
            print(
                f"Neo4j: FAILED ({type(exc).__name__}: {exc}). "
                f"Is the stack running? Start it with: uv run start.py",
                file=sys.stderr,
            )
            return 1
        print(f"Neo4j: deleted {removed} node(s).")

    if not args.neo4j_only:
        try:
            deleted = clear_qdrant()
        except Exception as exc:  # noqa: BLE001
            print(
                f"Qdrant: FAILED ({type(exc).__name__}: {exc}). "
                f"Is the stack running? Start it with: uv run start.py",
                file=sys.stderr,
            )
            return 1
        if deleted:
            print(f"Qdrant: deleted collection(s) {', '.join(deleted)}.")
        else:
            print("Qdrant: no collections existed - already empty.")

    manifest = ROOT / "index_manifest.json"
    if manifest.exists():
        manifest.unlink()
        print(f"Removed {manifest.name} (described a corpus that no longer exists).")

    print("\nDone. Run `uv run index.py <path>...` to rebuild.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
