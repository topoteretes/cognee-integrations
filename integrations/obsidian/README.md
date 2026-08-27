# Obsidian → Cognee Vault Exporter (POC)

A lightweight script that walks an Obsidian vault and ingests it into [Cognee](https://www.cognee.ai/), preserving the vault's own structure: frontmatter, tags, and wikilinks. 

## What it does

1. **Walks the vault** — recursively finds every `.md` file, skipping `.obsidian/`, `.trash/`, and a configurable templates folder by default
2. **Preserves structure:**
   - YAML frontmatter is split out and folded into a short metadata header (title, servings, source, etc.) prepended to each note's body
   - Tags — both frontmatter `tags:` lists and inline `#tags` — are merged, deduplicated, and passed to Cognee as `node_set`
   - `[[wikilinks]]` are left untouched in the note body sent to Cognee. Cognee's `cognify()` step reads them directly and reliably turns them into graph edges
3. **Ingests via the Cognee SDK** — `cognee.add()` per note, dataset named after the vault folder, followed by a single `cognee.cognify()` call once every changed note has been added
4. **Idempotent re-runs** — a local `manifest.json` maps each note's vault-relative path to a SHA-256 hash of its full raw text. Unchanged notes are skipped on subsequent runs; only new or edited notes are re-sent to Cognee
5. **A graph visualization and a cross-link search demo** — see below.


## Install
 
From a checkout of this repository:
 
```bash
cd integrations/obsidian
pip install -e .
```
 
This pulls in `cognee` (pinned `>=0.5.1,<0.6.0`) and `pyyaml`. If you'd rather manage the venv yourself:
 
```bash
uv venv
source .venv/bin/activate
uv pip install -e .
```
 
## Configure
 
Copy the example env file and fill in your values:
 
```bash
cp .env.example .env
```
 
Set your LLM API key (Cognee defaults to OpenAI for both completion and embeddings):
 
```bash
export LLM_API_KEY="sk-..."
```

Point the script at your vault ('VAULT' is required):

```bash
export VAULT="/path/to/your/vault"
```

### Configuration reference
 
| Setting | Env var | Default |
|---|---|---|
| Vault path | `VAULT` | *(required, no default)* |
| Manifest path | `MANIFEST_PATH` | `.cognee-manifest-<vault-name>.json` |
| Graph output | `GRAPH_OUTPUT` | `graph.html` |
 
The manifest is namespaced per-vault by default, so running this against multiple vaults from the same working directory won't collide. Override `MANIFEST_PATH` if you want it stored elsewhere, e.g. inside the vault itself.
 

## Usage

```bash
cognee-obsidian-export
```
 
or, without installing the console script:
 
```bash
python -m cognee_integration_obsidian.traversal
```
 

First run ingests every note. Subsequent runs only re-ingest notes that have actually changed, based on content hash. This also writes `graph.html` (an interactive visualization of the resulting knowledge graph) to the current directory, accesible via browser.

**Raw graph context (`only_context=True`)** confirms the actual traversal, not just a plausible-sounding answer

**Polished answer (`only_context=False`)** uses the LLM, quicker to hallucinate 

## Known limitations (POC scope)

- **Wikilinks are extracted via regex, not a real markdown parser.** Fenced code blocks containing literal `[[...]]` or `#tag`-shaped text will be picked up as if they were real
- **Embeds (`![[Note]]`) are indistinguishable from links (`[[Note]]`)** in this script's own parsed link list, though Cognee's extraction still handles the underlying text fine either way
- **Duplicate note titles across folders are not disambiguated** by this script's own link-cleaning step 
- **Heading/block-reference links** (e.g. `[[Note#Heading]]`) can produce duplicate, overlapping graph nodes (a plain `Note` node alongside a separate `Note#Heading` node) rather than one clean node with an internal reference
- **Relationships come from Cognee's own LLM-based extraction of raw wikilink text**, not from explicit, hand-built graph edges. Relationship accuracy depends on the LLM correctly interpreting `[[...]]` syntax, not guaranteed structurally
- **A permanently deleted note (removed with nothing replacing it) is not removed from Cognee.** This script's local manifest correctly forgets it, but nothing tells Cognee's own dataset to forget it too — there's currently no code path that calls Cognee's `forget()` API for this case. Note this is narrower than it might sound: a **deleted-then-restored** note with unchanged content was tested directly and produces **no duplication**. Cognee's own content-hash deduplication and internal orphan cleanup handle that case correctly on their own
- **Text notes only** — attachments (PDFs, images) are out of scope for this version
- **No live sync** — this is a one-shot export you re-run manually, not a file watcher
- **Not published as an Obsidian community plugin** — this is a script you run yourself, not something installed from within Obsidian

## Possible next steps

- Build explicit `DataPoint`/`Edge` objects from the vault's real link graph for guaranteed structural relationships, rather than relying entirely on LLM inference from raw text
- Fence-aware tag/link extraction to eliminate code-block false-positive case 
- Disambiguate duplicate titles using full relative paths when a plain title is ambiguous

## Development
 
```bash
cd integrations/obsidian
uv sync --dev
uv run pytest -q
```