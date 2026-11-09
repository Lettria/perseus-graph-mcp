"""Bring up the local stack (Neo4j + Qdrant ) and wait until it is usable.

Run with:  uv run start.py

"""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")


def wait_for(name: str, probe, timeout: int) -> bool:
    """Poll `probe` until it returns a truthy status string, or time out.

    `probe` returns a detail string on success and raises on failure.
    """
    deadline = time.time() + timeout
    last_error = ""
    while time.time() < deadline:
        try:
            detail = probe()
            print(f"  ok    {name}: {detail}")
            return True
        except Exception as exc:  # noqa: BLE001 - any failure means "not ready yet"
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(2)
    print(f"  FAIL  {name}: still unreachable after {timeout}s ({last_error})")
    return False


def probe_neo4j():
    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI", "neo4j://localhost:7687")
    auth = (
        os.environ.get("NEO4J_USER", "neo4j"),
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()
        with driver.session() as session:
            # .single() is typed Record | None, hence the explicit check.
            record = session.run("MATCH (n) RETURN count(n) AS c").single()
            nodes = record["c"] if record else 0
    return f"{uri}, {nodes} nodes"


def probe_qdrant():
    url = os.environ.get("QDRANT_URL", "http://localhost:6333")
    resp = requests.get(f"{url}/readyz", timeout=5)
    resp.raise_for_status()
    collections = requests.get(f"{url}/collections", timeout=5).json()
    names = [c["name"] for c in collections["result"]["collections"]]
    return f"{url}, collections: {names or '(none yet)'}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="seconds to wait per service (default: 300)",
    )
    parser.add_argument(
        "--down", action="store_true", help="stop the stack instead of starting it"
    )
    args = parser.parse_args()

    if args.down:
        return subprocess.run(["docker", "compose", "down"], cwd=ROOT).returncode

    print("Starting containers...")
    result = subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT)
    if result.returncode != 0:
        print("docker compose failed; is the Docker daemon running?", file=sys.stderr)
        return result.returncode

    print("\nWaiting for services...")
    ok = [
        wait_for("neo4j", probe_neo4j, args.timeout),
        wait_for("qdrant", probe_qdrant, args.timeout),
    ]

    if not all(ok):
        print(
            "\nSome services are not ready. Logs: docker compose logs", file=sys.stderr
        )
        return 1

    print("\nStack is up. Next: uv run index.py <folder-of-documents>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
