# perseus-graph-mcp

An MCP server that lets Claude query a knowledge graph built by
[Perseus](https://docs.perseus.lettria.net/) from your own documents, using a two-level
vector search — per-entity and per-topic — over Neo4j and Qdrant to pull back just the
relevant subgraph for a question (see [How retrieval works](#how-retrieval-works)).

Indexing is slow — Perseus uploads each file, submits a job and polls it — so it does **not**
happen inside a tool call. It runs offline as a CLI step, and the MCP server only does fast
read-only retrieval against the already-populated databases.

```
start.py   docker compose up: Neo4j + Qdrant, wait until healthy
index.py   files and/or folders (.md/.txt/.pdf)  →  Perseus  →  Neo4j  →  retrieval index
server.py  MCP: search_graph(query) → matching subgraph as Markdown
```

## 0. Install

You need two things on your machine: **uv** and **Docker**.

### uv

Runs the Python scripts and manages their dependencies — installs Python 3.11+ itself if you
don't already have it, so there's nothing else Python-related to set up.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh        # macOS / Linux
```

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # Windows
```

On macOS, Homebrew works too and sidesteps the gotcha below entirely:

```sh
brew install uv
```

**If `uv --version` then says "command not found," the install almost certainly worked
anyway** — the installer adds `uv` to PATH by editing your shell's profile (`~/.zshrc` on
macOS), but your _current_ terminal doesn't reload that file on its own. Open a new terminal
window (or run `source ~/.zshrc`) and try again before assuming the install failed.

### Docker

Runs Neo4j and Qdrant locally. (Embeddings run on Gemini's API, not a local container — see
[Configure](#1-configure).)

On macOS and Windows this means **Docker Desktop** — a GUI app, not just a CLI package.

**macOS**: [download it](https://docs.docker.com/desktop/setup/install/mac-install/) (there
are separate builds for Apple Silicon and Intel — pick the one matching your Mac), drag it
into Applications, then **open it once** so Docker actually starts; look for the whale icon
in the menu bar. Installing the app is not enough — `docker` and `docker compose` won't
respond until Docker Desktop is actually running, and that's the single most common reason
this step looks broken. Homebrew can fetch the same app:

```sh
brew install --cask docker-desktop
```

(still the same GUI app underneath — this only automates placing it in Applications; you
still need to open it once afterward.)

**Windows**: [download it](https://docs.docker.com/desktop/setup/install/windows-install/)
and run the installer. Same rule applies — it has to actually be running (check the system
tray for the whale icon) before `docker` commands work.

**Linux**: install `docker` and the `docker compose` plugin from your distribution's package
manager, or follow [Docker's own instructions](https://docs.docker.com/engine/install/). No
GUI app here — the `docker` service just needs to be running (`systemctl status docker`).

Verify both tools, from a new terminal if you just installed uv:

```sh
uv --version
docker info
```

`docker info` hangs or errors until Docker Desktop (or the Linux daemon) is actually running.

From the repo root, pull in uv's dependencies once:

```sh
uv sync
```

(`uv run` does this automatically on first use too, so this step is optional — but it's a good
way to confirm everything resolves before you start the stack.)

## 1. Configure

```sh
cp sample.env .env
```

Then set both `PERSEUS_API_KEY` and `GEMINI_API_KEY` in `.env` — **both are required**.
Perseus builds the graph; Gemini (`gemini-embedding-001`) embeds it, for every `index.py`
run and every single `search_graph` call, not just the two things it used to be optional
for. Every other default already matches `docker-compose.yml`. All three scripts load this
file by absolute path, so they work whatever directory they are launched from.

`GEMINI_API_KEY` also still covers PDF conversion (read whenever a PDF actually needs
converting) and NL2Cypher (read by `server.py` on every search — see
[How retrieval works](#how-retrieval-works)), which used to be the only things it did. Now
that embeddings depend on it too, there's no skipping it the way the old optional framing
suggested.

## 2. Start the stack

```sh
uv run start.py
```

Brings up Neo4j (7474/7687) and Qdrant (6333), then waits for both. There's no local
embedder to wait for anymore — embeddings run on Gemini's API (see
[Configure](#1-configure)) — so this is quick once the images are pulled.

`uv run start.py --down` stops everything.

## 3. Index some documents

```sh
uv run index.py ./notes
```

Each argument is either a folder (scanned recursively for `.md`/`.txt`/`.pdf`;
`--no-recursive` to stay flat) or an individual file — mix both in one run, e.g.:

```sh
uv run index.py --ontology schema.ttl notes/report.md notes/background.pdf
```

Builds a graph per file, writes it into Neo4j, then builds the retrieval index. The same
file reachable twice (named directly and also inside a folder also passed) is only indexed
once.

**PDFs are converted first.** Perseus ingests text, so each `.pdf` is sent to Gemini and
written back as a `.md` file _next to the PDF_, which the scan then picks up like any other
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

## Clearing the databases

```sh
uv run clear.py
```

Deletes every node from Neo4j and the `chunks`/`communities`/`schema` collections from
Qdrant, then removes `index_manifest.json`. Prompts for confirmation first (`-y`/`--yes` to
skip it); `--neo4j-only` or `--qdrant-only` narrows it to one database.

This is `--fresh` without the indexing attached — reach for it when you want the databases
empty right now, not just emptied as a side effect of the next `index.py` run. The source
documents are untouched either way, and Perseus's job history lives server-side, so
`index.py <path>...` afterward rebuilds without re-processing or re-billing.

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

## How retrieval works

`index.py` builds two separate vector indexes over the graph, and every `search_graph` call
searches both before walking the graph itself:

- **Entity level, in the `chunks` collection.** Every node gets a short text "chunk"
  describing its labels, properties, and up to 10 neighboring relationships, embedded and
  indexed. This is what makes "tell me about X" questions work.
- **Topic level, in the `communities` collection.** The Leiden algorithm partitions the whole
  graph into communities — clusters of nodes more densely connected to each other than to the
  rest of the graph — and each community gets a short summary of its most-connected members,
  embedded separately. This is what lets a broader question ("what does Lettria do in
  retail?") still find something, even when no single entity's chunk text is a close match.

Both kinds of text are truncated to 6,000 characters before embedding (`embedder.py`'s
`make_gemini_embed_fn`). Gemini's embedding model caps input at ~2,048 tokens and — confirmed
by testing, not documented anywhere — doesn't error past that: it silently drops the tail
instead, so an oversized chunk would otherwise lose part of its content to search with no
sign anything happened. 6,000 chars leaves real margin under that limit; a warning is logged
whenever a text actually needs cutting. Today's real chunks are nowhere near that size, since
`simple-graph-retriever`'s own chunk-building caps context at 10 relationships per node.

A `search_graph` call runs this pipeline:

1. Embed the query — once, as a single vector.
2. Vector-search `communities` for the top `max_communities` matches, and `chunks` for the
   top `max_chunks` matches (narrowed to just those communities, when any matched). Both
   searches apply a score threshold and stop early once the gap between consecutive scores
   widens past a drop-off percentage, so a few strong matches aren't diluted by weak ones.
3. Expand each matched community into up to `community_expansion_limit` of its member nodes,
   and combine those with the chunk-matched nodes into one seed set.
4. Walk the graph outward from every seed, up to `max_hops` hops, collecting every node and
   relationship it touches along the way. `allowed_rel_types` / `denied_rel_types` restrict
   which relationships that walk is allowed to follow.
5. Return the resulting subgraph as Markdown, with the chunk/community scaffolding stripped
   back out — only your actual entities and relationships remain.

So `max_chunks` / `max_communities` control how many seeds the search starts from, and
`max_hops` controls how far the graph is explored from each one — raising either pulls in
more context at the cost of a longer response. It's also why a query should cover one
subject at a time (see the tool description for examples): step 1 embeds the whole question
into a single vector, so asking about two unrelated things at once produces a vector that
matches neither well.

simple-graph-retriever also supports an LLM-driven step (NL2Cypher) that generates and runs
Cypher directly against the graph's schema, merging matching nodes into the same seed set as
the vector search above. This server turns it on automatically whenever `GEMINI_API_KEY` (or
`GOOGLE_API_KEY`) is set — `embedder.py`'s `make_llm_fn()` wraps Gemini for it, same model as
PDF conversion. Now that embeddings run on Gemini too, that key is required regardless, so in
practice this is always on; the `use_nl2cypher=False` fallback in `search_graph` still exists
in code for a key-less setup, it just isn't a configuration anyone running this server
actually has anymore.

A failed Gemini call (bad key, rate limit, network) doesn't fail the search: `make_llm_fn()`
catches it and returns `"NONE"` — the sentinel the SDK's own prompt already uses for "nothing
relevant in the schema" — so that call's vector-search results still come back normally.

## The tools

### search_graph

`search_graph(query, max_hops=1, max_chunks=10, max_communities=3, allowed_rel_types=None,
denied_rel_types=None)` returns the subgraph from the pipeline above as Markdown — one
section per entity with its properties and outgoing relationships, compact enough to drop
straight into a conversation.

### run_cypher

`run_cypher(query, parameters=None, max_rows=100)` runs a Cypher query directly against
Neo4j and returns the rows as JSON — for what vector search structurally can't do: exact
counts, aggregates, exhaustive lists, or a specific multi-hop pattern you can name yourself.
`search_graph` stays the right tool for anything that's really a semantic question.

**Read-only, enforced by Neo4j itself, not a client-side filter.** The session it runs in is
opened in read access mode, so the server rejects any `CREATE`/`MERGE`/`DELETE`/`SET`/`REMOVE`
before it reaches the graph — confirmed directly: a `CREATE` through a read-access session
comes back `Neo.ClientError.Statement.AccessMode`, with the node count unchanged. There is no
way to use this tool to modify data, by design, not by convention.

Every query also gets a fixed 10-second server-side timeout (confirmed the same way: a
deliberately expensive multi-way cartesian product gets cancelled with
`TransactionTimedOutClientConfiguration` before producing a row) and a `max_rows` cap on what
comes back — the two bound different things. The timeout bounds how much work Neo4j does; an
unindexed `MATCH` over the whole graph can exceed it long before `max_rows` rows exist to
return. Write your own `LIMIT` for an exploratory query rather than leaning on `max_rows`.

Like `search_graph`'s subjects list, the tool description names the graph's actual node
labels and relationship types (from `index_manifest.json`, same mechanism as below), so the
model writes Cypher against this graph's real schema instead of guessing generic ones.

### How the tool descriptions are generated

Both tool descriptions and the server's `instructions` come from `index_manifest.json` —
written by `index.py`'s `write_manifest()` after indexing, from a few Neo4j queries: the most
common entity labels and relationship types, up to 12 topics drawn from the `:Community.text`
summaries the retriever already computed (each with a handful of key entity names), and the
list of source documents. `server.py` reads that file and composes two different views of it:

- The **tool description** is written prescriptively, not just informatively — "USE THIS
  FIRST... before answering from your own knowledge" — and lists the actual subjects, entity
  types, and relationship types present, so the model can tell whether a question is even
  answerable from this graph before calling it. Without that framing, Claude tends to answer
  from training data and only mention the connector afterwards.
- The server's **`instructions`** (sent once, in the MCP `initialize` response) carry a
  shorter version of the same pitch as standing context about the server as a whole, not
  about this one tool.

A tool description sits in the prompt prefix for every request in a conversation, not paid
once like a result, so this stays capped at ~1200 characters rather than listing everything —
document names beyond the first 10 collapse into a count.

**Both are computed once, at server startup** — `@mcp.tool(description=...)` and
`MCPServer(instructions=...)` evaluate them as the server starts, not per call. Re-run
`index.py` and the description won't reflect the new corpus until the server restarts: fully
quit and reopen Claude Desktop, or hit Restart in the Inspector.

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

Lists both tools and lets you call either with arbitrary arguments.

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
— simple-graph-retriever has had bare `print()`s on both its retrieval and indexing paths
before (fixed upstream since, in rc26 and rc27 respectively), so this stays in place as cheap
insurance against a future one landing here again.
