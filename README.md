# perseus-graph-mcp

An MCP server that lets Claude query a knowledge graph built by
[Perseus](https://docs.perseus.lettria.net/) from your own documents.

Indexing is slow — Perseus uploads each file, submits a job and polls it — so it does **not**
happen inside a tool call. It runs offline as a CLI step, and the MCP server only does fast
read-only retrieval against the already-populated databases.

```
start.py   docker compose up: Neo4j + Qdrant + embedder, wait until healthy
index.py   folder of .md/.txt/.pdf  →  Perseus  →  Neo4j  →  retrieval index
server.py  MCP: search_graph(query) → matching subgraph as Markdown
```

## 0. Install

You need two things on your machine:

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — runs the Python scripts
  and manages their dependencies. Installs Python 3.11+ itself if you don't already have it,
  so there's nothing else Python-related to set up.

  ```sh
  curl -LsSf https://astral.sh/uv/install.sh | sh        # macOS / Linux
  ```

  ```powershell
  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
  ```

- **[Docker](https://docs.docker.com/get-docker/)** (with Compose, included in Docker Desktop) —
  runs Neo4j, Qdrant and the embedder locally.

From the repo root, pull in the dependencies once:

```sh
uv sync
```

(`uv run` does this automatically on first use too, so this step is optional — but it's a good
way to confirm everything resolves before you start the stack.)

## 1. Configure

```sh
cp sample.env .env
```

Then set `PERSEUS_API_KEY` in `.env`. Every other default already matches
`docker-compose.yml`. All three scripts load this file by absolute path, so they work
whatever directory they are launched from.

Set `GEMINI_API_KEY` too if you want to index PDFs — it is only read when a PDF actually
needs converting, so leave it blank if you only have `.md` and `.txt`.

## 2. Start the stack

```sh
uv run start.py
```

Brings up Neo4j (7474/7687), Qdrant (6333) and a TEI embedder (8080), then waits for all
three. The embedder probe also checks that the model's vector width matches `VECTOR_SIZE`,
which otherwise shows up much later as searches that silently return nothing. The first run
downloads the embedding model, so allow a few minutes.

`uv run start.py --down` stops everything.

## 3. Index some documents

```sh
uv run index.py ./notes
```

Picks up every `.md` and `.txt` file under the folder (recursively; `--no-recursive` to stay
flat), builds a graph per file, writes it into Neo4j, then builds the retrieval index.

**PDFs are converted first.** Perseus ingests text, so each `.pdf` is sent to Gemini and
written back as a `.md` file *next to the PDF*, which the scan then picks up like any other
source. The Markdown is a normal file you can read, edit or delete.

Conversion is the one step that costs money per call rather than being reused, so a PDF is
skipped whenever its `.md` already exists and is newer — re-running `index.py` does not
re-convert anything. Use `--no-pdf` to ignore PDFs entirely.

Expect a condensed rendering rather than a verbatim transcription — the conversion prompt
asks for headings, tables and lists, and the model summarises prose. Skim the generated `.md`
before indexing if fidelity matters. `GEMINI_MODEL` overrides the model, which defaults to
`gemini-3-flash-preview`.

Re-running is cheap and safe. Perseus reuses an already-succeeded job for an unchanged file,
so nothing is re-processed or re-billed, and the Neo4j load `MERGE`s on entity uuid, so
repeat runs do not duplicate entities. Files you delete from the folder keep their nodes.

Two things worth knowing:

- The final step **always rebuilds the retrieval layer from scratch** — it recreates both
  Qdrant collections and deletes every `:Community` and `:GraphChunk` node. Only the
  entity/relation load is additive.
- `--refresh` forces new Perseus jobs. It can mint new uuids for the same content, which
  _can_ duplicate entities — the one case where re-running is not idempotent.

Before loading, the per-file graphs are merged with `perseus_client.interlink()`, keyed on
`rdfs:label`, so an entity mentioned in several documents becomes one node rather than one per
document. Only the merged graph is written to Neo4j. Note that interlink merges entities
sharing a label **and** a type — the same entity extracted under two different ontology
classes stays as two nodes, which an `--ontology` constraining the classes is the real fix
for.

| Flag                     | Effect                                                   |
| ------------------------ | -------------------------------------------------------- |
| `--ontology PATH`        | Constrain extraction with a TTL/OWL ontology             |
| `--refresh`              | Force new Perseus jobs instead of reusing succeeded ones |
| `--no-recursive`         | Do not descend into subfolders                           |
| `--no-pdf`               | Ignore PDFs instead of converting them                   |
| `--reconvert-pdf`        | Re-convert PDFs even when an up-to-date `.md` exists     |
| `--convert-only`         | Convert PDFs to Markdown and stop, without indexing      |
| `--no-interlink`         | Load the per-file graphs as-is, without deduplicating    |
| `--fresh`                | Delete every Neo4j node first, for a clean rebuild       |
| `--skip-retrieval-index` | Load Neo4j only, skip the community/chunk rebuild        |

Use `--fresh` after removing or renaming source documents: the load is additive, so interlink
only sees the graphs from the current run and cannot retract a node written by an earlier one.
Job reuse makes the rebuild free.

## 4. Add it to Claude Desktop

Run the line for your platform from the repo root. Each one **replaces** the config file, so
if you already have other MCP servers configured, add the entry by hand instead (see below).

Linux:

```sh
echo '{"mcpServers":{"perseus-graph-mcp":{"command":"uv","args":["run","--directory","'"$PWD"'","server.py"]}}}' > ~/.config/Claude/claude_desktop_config.json
```

macOS:

```sh
echo '{"mcpServers":{"perseus-graph-mcp":{"command":"uv","args":["run","--directory","'"$PWD"'","server.py"]}}}' > ~/Library/Application\ Support/Claude/claude_desktop_config.json
```

Windows (PowerShell):

```powershell
echo "{""mcpServers"":{""perseus-graph-mcp"":{""command"":""uv"",""args"":[""run"",""--directory"",""$($PWD.Path -replace '\\','/')"",""server.py""]}}}" | Set-Content -Encoding utf8 "$env:APPDATA\Claude\claude_desktop_config.json"
```

Then fully quit and reopen Claude Desktop, click "Add files, connectors, and more" at the
bottom-left of the message box, hover "Connectors" and choose "Manage connectors" to confirm
`perseus-graph-mcp` is listed with its `search_graph` tool. Now ask a question about the
documents you indexed.

If it does not connect, replace `"uv"` with the absolute path from `which uv` (`where.exe uv`
on Windows) — Claude Desktop starts with a minimal `PATH` and may not find a bare `uv`.

<details>
<summary>Or edit the config by hand</summary>

The commands above write this to `~/.config/Claude/claude_desktop_config.json` (Linux),
`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS), or
`%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "perseus-graph-mcp": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--directory", "/absolute/path/to/this/repo", "server.py"]
    }
  }
}
```

Both paths must be absolute: the server is launched from an arbitrary working directory, and
Claude Desktop starts with a minimal `PATH`, so a bare `uv` will not be found. Settings →
Developer → Edit Config opens this same file — the app has no form for adding a local
server, only for remote URL connectors.

</details>

## The tool

`search_graph(query, max_hops=1, max_chunks=10, max_communities=3, allowed_rel_types=None,
denied_rel_types=None)` returns the relevant subgraph as Markdown — one section per entity
with its properties and outgoing relationships, which is compact enough to drop straight into
a conversation. Raise `max_hops` for more context at the cost of much more text.

The tool description and the server's `instructions` both tell the model to search before
answering from its own knowledge, and both list the subjects actually present in the graph —
generated from `index_manifest.json`, so they describe _your_ corpus. Without that, Claude
tends to answer from training data and only mention the connector afterwards.

If it still answers from memory, add a line to your own Claude instructions (profile
preferences, or a Project's instructions) such as _"for questions about Lettria, always query
the perseus-graph-mcp connector first"_. A user instruction outweighs anything a server can
say about itself.

An empty result is always explained rather than returned as a bare "nothing found": the
server probes the embedder and Qdrant to tell apart _embedder down_, _nothing indexed yet_,
and _a genuine miss_. The underlying library reports all three identically, so this
distinction is the difference between a two-minute fix and an hour of guessing.

## Testing it without Claude

```sh
npx @modelcontextprotocol/inspector uv run server.py
```

Lists the tools and lets you call `search_graph` with arbitrary arguments.

## Logs

The server logs to stderr, which Claude Desktop captures per server:

```sh
tail -f ~/.config/Claude/logs/mcp-server-perseus-graph-mcp.log     # Linux
tail -f ~/Library/Logs/Claude/mcp-server-perseus-graph-mcp.log     # macOS
```

`~/.config/Claude/logs/mcp.log` has the other side of the conversation — the client's
handshake and every message exchanged.

Log to stderr, never stdout: stdout carries the JSON-RPC protocol, so a bare `print()` in a
tool will corrupt it and the server will drop off with a parse error. `server.py` wraps the
retrieval call in `contextlib.redirect_stdout(sys.stderr)` as a defensive measure against this
— the retriever library's indexing path still prints progress, so this guards against a
future print landing on the retrieval path too.

## Development

`artifact/` holds reference clones of `perseus-client` and `simple-graph-retriever`. It is
gitignored and not used at runtime; both are installed from PyPI via `pyproject.toml`.

Requires Python 3.11+ — `simple-graph-retriever` imports `typing.LiteralString`, which does
not exist before 3.11 (its own metadata claims 3.8+).

`simple-graph-retriever` 1.0.0rc26 stopped bundling an embedding provider: `GraphRetrievalClient`,
`GraphIndexer` and `GraphRetriever` now all require an `embed_fn` callback
(`list[str] -> list[list[float]]`) from the caller instead of talking to `EMBEDDER_URL`
themselves. `embedder.py` supplies one backed by the local TEI service, shared by both
`index.py` and `server.py`. This is a hard dependency floor (`>=1.0.0rc26` in
`pyproject.toml`), not just a recommendation — earlier versions don't accept the `embed_fn`
argument at all.
