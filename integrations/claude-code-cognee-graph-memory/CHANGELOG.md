# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.1] - 2026-05-04

### Changed
- Code style cleanup only — **no behavior changes**. v0.2.0 verification
  results (UT 110 / IT 6 / ET 4 / ST 4 all passed; qwen2.5:14b matrix
  verification 8 tools × 5 runs) remain valid because no logic was modified.
- Applied Ruff lint fixes to comply with cognee-integrations coding
  standards (`line-length = 100`, `select = ["E", "F", "I", "W"]`,
  `target-version = "py310"`):
  - `harness/hooks/auto_remember_completion.py`: F401 — unused
    `import os` commented out and moved out of import block.
  - `harness/hooks/auto_remember_user_message.py`: F401 — unused
    `import os` and `import subprocess` commented out and moved out
    of import block.
  - `harness/hooks/cognee_remember_flusher.py`: F841 — `result =`
    assignment commented out (function call retained); E501 — argparse
    `--interval` line wrapped to fit 100-char limit.
  - `src/knowledge_src/import_knowledge.py`: E501 — five logger and
    argparse lines wrapped (one log message wording slightly shortened
    while preserving meaning: "the failure" → "failure", "the cause"
    → "cause").
  - `src/main_src/import_to_graph.py`: I001 — `urllib.error` and
    `urllib.request` imports reordered alphabetically; E501 — argparse
    description and three help lines wrapped (one description shortened
    while preserving meaning: "production runtime" → "runtime").

### Why
- Preparing the toolkit for potential cognee-integrations contribution
  (per cognee co-founder Vasilije Markovic's invitation on X to send a
  PR with toolkit features). The cognee-integrations CI enforces
  Ruff lint rules above; passing those rules ahead of time avoids
  CI rejections during the PR review process.

## [0.2.0] - 2026-05-04

### Added
- New `knowledge/sample_knowledge/05_graph_memory_operations.md`: a sample
  ingestion file documenting operational know-how for Cognee graph memory
  (when to call cognify, remember vs. save_interaction, search-type choice,
  recall auto-routing, and forget_memory/improve/prune usage). The bundled
  sample count therefore increased from 4 to 5 files.

### Changed
- **Upgraded Cognee from 1.0.3 to 1.0.5**. Cognee 1.0.4 replaced the embedded graph
  database from KuzuDB to **Ladybug DB**, and this distribution follows that change.
  - Dependency: `kuzu==0.11.3` → `ladybug==0.16.0` (auto-replaced; backward-compatible aliases retained)
  - Verified Cognee version: 1.0.5
  - cognee-mcp version: 0.5.4 (no change)
- Updated documentation to replace **"KuzuDB" with "Ladybug DB"** throughout:
  - `README.md` (features and tech stack table)
  - `config/.env.example` (COGNEE_DATA_PATH comments and storage description)
  - `docs/SETUP.md` (verified versions and pinning examples)
  - `docs/GETTING_STARTED.md` (verified Cognee version and measured response times)
- Updated the bundled-sample file-count notation from 4 to 5:
  - `README.md` (directory structure section)
  - `docs/GETTING_STARTED.md` (including the `[N/M]` ingestion log examples)
- Reorganized design-decision notes in
  `knowledge/sample_knowledge/03_design_decisions.md`. KuzuDB/LanceDB/FastEmbed
  are bundled with Cognee and used automatically, so they are not user-side
  selections. The text was consolidated under "Why Cognee was adopted" and
  notes that the graph DB is Ladybug DB in v0.2.0 / KuzuDB in v0.1.x.
- Updated error-handling notes in
  `knowledge/sample_knowledge/04_common_errors.md` so that local-LLM examples
  use `qwen2.5:14b` (the only locally verified LLM for v0.2.0) instead of
  `llama3.1:8b`.
- Updated the recall failure-condition note in
  `harness/rules/cognee_memory_usage.md` from "may fail when running on
  `llama3.1:8b`" to "may fail when running on local LLMs other than
  `qwen2.5:14b`".

### Fixed (pre-existing source code defects from v0.1.10–v0.1.12)
- `src/main_src/import_to_graph.py`:
  - Removed a stale display line in `list_targets()` for the `comments` target
    that was not defined in `TARGET_MAP` (a leftover from v0.1.10).
  - `check_ollama()` now runs only when `LLM_PROVIDER=ollama`
    (previously it failed unnecessarily when a cloud API was configured).
  - `check_ollama()` is now skipped during `--dry-run`
    (the dry-run mode only prints the file list and does not need Ollama).
- `src/knowledge_src/import_knowledge.py`:
  - Added the same `LLM_PROVIDER=ollama` guard to `check_ollama()`.
- `harness/hooks/auto_remember_user_message.py`:
  - Removed contradictory docstring lines that claimed "this sample provides the
    simpler direct-call variant" while the actual implementation uses the queue
    approach exclusively.
- `harness/hooks/cognee_remember_flusher.py`:
  - Fixed the `remaining` filter from `not line.strip()` to `line.strip()`
    (an internal bug that caused failed entries to be silently dropped from
    the queue; the data itself was still preserved in `failed.jsonl`, but
    the queue — the source of retry attempts — held the wrong contents).
  - Removed an unnecessary `sys.path.insert(main_src)` in `remember_via_mcp()`
    (fastmcp is importable directly from the distribution's venv site-packages).

### Verified (in v0.2.0)
- **qwen2.5:14b (num_ctx=8192) × Ladybug DB: 35/40 ✅** verified
  - remember 5/5 ✅, search(CHUNKS) 5/5 ✅, search(GRAPH_COMPLETION) 5/5 ✅, recall 5/5 ✅
  - cognify 5/5 ✅, improve 5/5 ✅, forget_memory 5/5 ✅
  - **save_interaction 0/5 ❌** (known limitation, see below)
- **Ladybug DB (introduced in Cognee 1.0.4) accelerates graph traversal**, making
  qwen2.5:14b practically usable (significant subjective improvement over the
  v0.1.x KuzuDB environment).
  - search(CHUNKS): avg 3.2s (deterministic, no LLM)
  - search(GRAPH_COMPLETION): avg 14.6s (range 12-18s)
  - recall (Q-A, TEMPORAL routing): 20-24s
  - recall (Q-B, GRAPH_COMPLETION_COT routing): 154-156s
  - improve / forget_memory: all immediate (under a few seconds)

### Known Issues
- **save_interaction is unavailable** (API mismatch between cognee-mcp 0.5.4 and cognee 1.0.5)
  - Error: `add_rule_associations() got an unexpected keyword argument 'context'`
  - Cause: cognee 1.0.5 renamed the `add_rule_associations` argument from `context` to `ctx`,
    but cognee-mcp 0.5.4 has not been updated (upstream `topoteretes/cognee` main branch is
    in the same state)
  - Workaround: Use `remember` for immediate persistence of interaction text

### Migration (from v0.1.x)
- Existing graph DB data (KuzuDB) from Cognee 1.0.3 is automatically migrated to Ladybug
  format on first startup of Cognee 1.0.4+.
- To preserve existing data: `pip install -U "cognee[fastembed]==1.0.5"` and start Cognee
  once → automatic migration runs.
- To reset data: run `forget_memory(everything=True)` to clear all data, then re-cognify
  in the Cognee 1.0.5 environment.

## [0.1.12] - 2026-05-03

### Fixed
- Hardware notation in `README.md` and `docs/GETTING_STARTED.md` is now
  expressed in terms of **VRAM capacity**, not GPU model name. Previous
  text said "RTX 4070 12GB or higher", but the laptop variant of the
  RTX 4070 has only 8GB of VRAM, which would mislead users into
  thinking their laptop met the requirement when it does not. The
  recommended threshold is now stated as "GPU with 12GB+ VRAM" with a
  note about laptop variants. The verified-minimum entry is now
  "NVIDIA GeForce RTX 4060 Laptop GPU (VRAM 8GB)" so the actual tested
  device is named precisely.
- `CHANGELOG.md` has been trimmed to keep only public-relevant
  entries (v0.1.10 onward); the pre-public v0.1.0..v0.1.9 history was
  internal development noise.

## [0.1.11] - 2026-05-02

### Changed
- Switched the default local LLM in `config/.env.example` to
  `qwen2.5:14b` (num_ctx=8192). Setup examples for Claude API and
  OpenAI API are also included as comments.
- Added a "Recommended LLM and Environment" section to
  `docs/GETTING_STARTED.md`.
  - Cloud APIs (Claude / OpenAI) are **strongly recommended** thanks
    to their official structured-output support.
  - Local LLM operation requires a GPU with **12GB+ VRAM** and
    **qwen2.5:32b or larger** (14B is the practical minimum).
- `src/main_src/import_to_graph.py` and
  `src/knowledge_src/import_knowledge.py` now read `LLM_MODEL` from
  `config/.env` dynamically (the previous `llama3.1:8b` hardcoding has
  been removed).
- Replaced `llama3.1:8b`-specific text in `docs/GETTING_STARTED.md`
  with `qwen2.5:14b` / cloud-API guidance (sample-ingestion failure
  fallback list, `recall` fallback notice, and the troubleshooting
  section).

## [0.1.10] - 2026-05-02

### Added
- Initial public release. A module that adds Cognee-based graph memory
  to Claude Code.
  - `src/main_src/start_cognee_mcp.py` — MCP server startup script
  - `src/main_src/import_to_graph.py` — production ingestion
  - `src/sample_src/load_sample.py` / `delete_sample.py` — bundled
    sample handling
  - `src/knowledge_src/split_knowledge.py` / `import_knowledge.py` —
    user-knowledge ingestion pipeline
  - `docs/SETUP.md` / `docs/GETTING_STARTED.md` /
    `docs/HARNESS_GUIDE.md` — setup, usage, and harness installation
    guides
  - `knowledge/sample_knowledge/` — 4 bundled sample files for
    end-to-end verification
  - `harness/` — Claude Code × Cognee auto-accumulation harness
    (optional)
  - Fully local operation possible (Ollama + FastEmbed, no external
    API keys required)
