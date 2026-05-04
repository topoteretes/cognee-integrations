# GETTING STARTED — Claude Code + Cognee Graph Memory: Operation Verification & Usage

This document explains how to verify the system after setup and how to ingest your own knowledge.

If you have not completed setup yet, please refer to `docs/SETUP.md` first.

---

## Recommended LLM and Environment

### Recommended LLM (strongly recommended)

To use all features (especially `recall` and `search(GRAPH_COMPLETION)`) **reliably**, we strongly recommend using a **cloud LLM API** that handles Cognee's structured output requirements consistently.

| Tier | LLM | Recommendation |
|------|-----|------|
| Cloud API (**strongly recommended**) | **Anthropic Claude API** (claude-sonnet-4-6, etc.) / **OpenAI API** (gpt-4o, etc.) | ★★★ Near-100% reliability with official structured-output support |
| Local LLM (conditionally OK) | qwen2.5:14b / qwen2.5:32b / qwen2.5:72b / llama3.3:70b — 14B or larger | ★★ Acceptable if your GPU has enough memory |
| Local LLM (**not recommended**) | Models smaller than qwen2.5:14b (llama3.1:8b / llama3.2:3b / gemma4:e4b, etc.) | ★ Frequent JSON Schema violations in structured output |

Local LLM operation avoids API billing, but its structured-output reliability is clearly inferior to cloud APIs. For production use or stable operation, choose a cloud API.

### Recommended Environment

| Item | Cloud API mode | Local LLM mode |
|------|---------------|---------------|
| GPU | Not required | **GPU with 12GB+ VRAM** recommended (note: laptop RTX 4070 has only 8GB and does NOT qualify; desktop RTX 4070 / 4070 SUPER / 4070 Ti / 4080 etc. do) |
| RAM | 16GB+ | **32GB+** |
| LLM | claude-sonnet-4-6 / gpt-4o, etc. | **qwen2.5:32b or higher** (14B+ is the minimum) |

Local LLM operation becomes practical with **a GPU that has 12GB+ VRAM**. With less VRAM (e.g. NVIDIA GeForce RTX 4060 Laptop GPU with 8GB VRAM), 14B-class models can still run, but model weights spill out of GPU memory and partially offload to CPU.

### Verification Record (reference)

This distribution's full feature set has been verified in the following environment:

- Test environment: GPU **NVIDIA GeForce RTX 4060 Laptop GPU (VRAM 8GB)** / RAM 32GB
- Test LLM: **qwen2.5:14b** (num_ctx=8192)
- Verified Cognee version: **1.0.5 (Ladybug DB)**
- Result: **35/40 success** (remember 5/5 ✅, search(CHUNKS) 5/5 ✅, search(GRAPH_COMPLETION) 5/5 ✅, recall 5/5 ✅, cognify 5/5 ✅, improve 5/5 ✅, forget_memory 5/5 ✅, **save_interaction 0/5 ❌** = known limitation, see below)
- Response time (measured on Ladybug DB):
  - search(CHUNKS): avg 3.2s (deterministic, no LLM)
  - search(GRAPH_COMPLETION): avg 14.6s (range 12-18s)
  - recall (Q-A, TEMPORAL routing): 20-24s
  - recall (Q-B, GRAPH_COMPLETION_COT routing): 154-156s (Chain-of-Thought reasoning)
  - improve / forget_memory: all immediate (under a few seconds)

**Ladybug DB (introduced in Cognee 1.0.4) accelerates graph traversal**, making GRAPH_COMPLETION and recall practically usable even with qwen2.5:14b (significant subjective improvement over the v0.1.x KuzuDB environment).

In other words, the distribution **runs all major features at practical speed on an 8GB-VRAM laptop GPU (RTX 4060 Laptop GPU) with qwen2.5:14b + Ladybug DB**.

### Default Configuration

The default `config/.env.example` ships with **qwen2.5:14b** (num_ctx=8192). If you want to use a cloud API, edit `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` / `LLM_ENDPOINT` (see `docs/SETUP.md` for details).

---

## Step 1: Verify operation with bundled samples

Run the following in your terminal.

```bash
cd <cloned directory>
src/venv/bin/python3 src/sample_src/load_sample.py
```

The five files under `knowledge/sample_knowledge/` will be ingested into Cognee one by one.

```
2026-04-29 10:00:00 [INFO] [1/5] 01_claude_code_tips.md → dataset=sample_knowledge
2026-04-29 10:00:30 [INFO] [2/5] 02_software_dev_lessons.md → dataset=sample_knowledge
...
```

Time estimate: 2-5 minutes (includes Ollama graph processing).

### If sample ingestion fails

When running on a local LLM, the model sometimes returns unstable responses that cause structured-output validation errors. After 5 retries the script may abort with errors such as `InstructorRetryException` or `Field required`. If this happens, clean up and retry:

```bash
# Remove any partially-ingested sample data
src/venv/bin/python3 src/sample_src/delete_sample.py

# Re-ingest the samples
src/venv/bin/python3 src/sample_src/load_sample.py
```

If the failure persists, check that Ollama is reachable (`ollama list` should show the model), try a larger local LLM (e.g. `qwen2.5:32b` / `qwen2.5:72b` / `llama3.3:70b`) by changing `LLM_MODEL` in `config/.env`, or **switch to a cloud API (Claude / OpenAI)** — this error rarely happens with cloud APIs.

---

## Step 2: Launch Claude Code and call MCP tools

Open a new Claude Code session (it can be a different session from the ingestion).

Try the following queries in the chat to retrieve knowledge from graph memory.

---

### Scenario A: Ask about Claude Code usage

**Your input:**

```
search("Tell me about the timing of git push", search_type="CHUNKS")
```

Or in natural language:

```
recall("When can I run git push?")
```

> ⚠️ With local LLMs (especially models 8B and below), `recall` may fail with an "LLM format error". If it fails, use `search(query, search_type="CHUNKS")` as a fallback (see Troubleshooting at the bottom of this file). Cloud APIs and qwen2.5:14b or larger handle `recall` reliably.

**Expected response:**

> "Run `git push` only when the user explicitly instructs to. Task or phase completion is not a reason to push."

---

### Scenario B: Look up past errors

```
search("How to handle errors when Ollama is unreachable", search_type="CHUNKS")
```

**Expected response:**

> "Run `ollama serve` and retry. Also check whether the configured local LLM (default: qwen2.5:14b) is downloaded with `ollama list`."

---

### Scenario C: Retrieve design rationale

```
search("Why KuzuDB is used", search_type="CHUNKS")
```

**Expected response:**

> "Python-native, in-process execution, bundled with Cognee (no extra installation). It was the only choice that satisfied the local-only and zero-cost requirements."

---

### Scenario D: Retrieve development lessons

```
search("Where to derive test expected values from", search_type="CHUNKS")
```

**Expected response:**

> "Unit-test expected values must be derived from the IF specification. Reading the implementation code to determine expected values is forbidden."

---

## Step 3: Register your own knowledge ad hoc (single record)

You can register knowledge gained on the fly by calling the `remember` tool from Claude Code.

```
remember("Today's lesson: Always take a backup before running Django migrate. There was a non-rollback-capable table change.", dataset_name="my_lessons")
```

In a later session:

```
recall("What should I watch out for with Django migrate?")
```

Returns the knowledge you registered.

> ⚠️ If `recall` fails with an "LLM format error", use `search("Django migrate", search_type="CHUNKS")` as a fallback.

---

## Step 4: Ingest your own knowledge in bulk

Steps to ingest existing knowledge files (`.md`).

### Step 4-1: Delete sample data (optional)

If you no longer need the bundled samples:

```bash
src/venv/bin/python3 src/sample_src/delete_sample.py
```

Only the `sample_knowledge` dataset is removed.

### Step 4-2: Place your knowledge under user_knowledge/

Place `.md` files under `knowledge/user_knowledge/`. You can use sub-folders by category.

```
knowledge/user_knowledge/
├── project-management/
│   └── task-management.md
├── design/
│   └── db-design.md
└── lessons/
    └── past-incidents.md
```

### Step 4-3: Split the knowledge

```bash
src/venv/bin/python3 src/knowledge_src/split_knowledge.py
```

Each `.md` under `user_knowledge/` is split by H2 heading and written to `knowledge/user_chunks/`.

### Step 4-4: Ingest the chunks

```bash
src/venv/bin/python3 src/knowledge_src/import_knowledge.py
```

Files in `user_chunks/` are ingested into Cognee one at a time, with retries on cognify failure.

Time estimate: tens of seconds to a few minutes per file. For 100 files, roughly tens of minutes to several hours.

### Step 4-5: Preview the ingestion list

```bash
src/venv/bin/python3 src/knowledge_src/import_knowledge.py --dry-run
```

A dry-run shows the file list that would be ingested.

---

## Tool reference

| Tool | Purpose | Example |
|-------|------|---|
| `remember(data, dataset_name)` | Register knowledge / decisions / lessons | `remember("...", dataset_name="lessons")` |
| `recall(query)` | Semantic search using graph + LLM (fallback to `search` on failure) | `recall("What were past incidents?")` |
| `search(search_query, search_type="CHUNKS")` | Retrieve text directly via vector search | `search("How to handle errors", search_type="CHUNKS")` |
| `list_data()` | Show registered datasets | `list_data()` |
| `prune()` | Reset all data | `prune()` |

---

## Troubleshooting

**`save_interaction` fails with `add_rule_associations() got an unexpected keyword argument 'context'`**
→ This is a **known limitation in v0.2.0**. API mismatch between cognee-mcp 0.5.4 and cognee 1.0.5
(cognee 1.0.5 renamed the `add_rule_associations` argument from `context` to `ctx`, but cognee-mcp
has not been updated yet).
As a workaround, use `remember(data="User: question\nAssistant: answer")` instead — this persists
the interaction text immediately into the permanent memory.

**SearchPreconditionError**
→ No data has been ingested yet. Run Step 1 first.

**`recall` returns empty results**
→ Graph processing might still be running. Check completion with `cognify_status()` and retry.

**LLM format error during `recall`**
→ Local LLMs (especially models 8B and below) sometimes do not respond in the JSON format Cognee expects. Use `search(query, search_type="CHUNKS")` as a fallback, or switch to a larger local LLM (qwen2.5:14b or above) or a **cloud API (Claude / OpenAI)**.

**Knowledge ingestion shows `status=errored`**
→ The file may be too large. Run `split_knowledge.py` to split it before ingesting. `import_knowledge.py` retries up to 3 times on failure.

---

## Appendix: v0.1.x (Cognee 1.0.3 / KuzuDB) Local LLM Comparison Data (Reference)

> This section is a reference record of the local-LLM comparison verification data from v0.1.x (KuzuDB environment). In v0.2.0, the graph DB has been replaced with Ladybug DB, and only qwen2.5:14b has been re-verified (see "Verification Results" near the top of this file). Use this section as background information when choosing a local LLM.

### Verification environment (v0.1.x)

- Cognee 1.0.3 / KuzuDB 0.11.3
- GPU: NVIDIA GeForce RTX 4060 Laptop GPU (VRAM 8GB) / RAM 32GB
- Verification date: 2026-05-02
- Runs per LLM: 4 tools × 5 runs = 20 runs

### LLM × Tool result summary

| LLM (num_ctx) | remember×5 | search(CHUNKS)×5 | search(GRAPH_COMPLETION)×5 | recall×5 | Total |
|---|---|---|---|---|---|
| llama3.1:8b (2048, initial) | 5/5 ✅ | 5/5 ✅ | 4/5 ⚠️ (#1: JSON Schema violation) | 1/5 ✅ + 2/5 ⚠️ + 2/5 ❌ (Q-B 2/2 fail) | 14/20 |
| llama3.1:8b (65536, retest) | 5/5 ✅ | 5/5 ✅ | 2/5 ✅ + 3/5 ❌ (pydantic ValidationError) | 2/5 ✅ + 3/5 ❌ (Q-B 2/2 fail continued) | 14/20 |
| llama3.2:3b (2048 default) | 0/5 ❌ Timeout | (skipped) | (skipped) | (skipped) | 0/20 |
| gemma4:e4b (16384) | 5/5 ✅ | 5/5 ✅ | **5/5 ✅** | 3/5 ✅ + 2/5 ❌ (Q-B 2/2 JSON Schema violation) | 18/20 |
| **qwen2.5:14b (8192)** | **5/5 ✅** | **5/5 ✅** | **5/5 ✅** | **5/5 ✅** (Q-A/Q-B all correct) | **20/20** |
| claude-sonnet-4-6 | (not run, to avoid API cost) | - | - | - | - |

### Key observations (v0.1.x)

- **qwen2.5:14b (num_ctx=8192) was the only LLM with a perfect score** (20/20). The only local LLM that fully answered recall Q-B (reasoning that includes background and rationale)
- **llama3.1:8b** did not improve with larger num_ctx; recall Q-B failed 2/2
- **llama3.2:3b** timed out at the connection test stage (too lightweight to complete entity extraction)
- **gemma4:e4b** scored full marks on GRAPH_COMPLETION but failed recall Q-B with JSON Schema violations
- This is the basis for shipping **qwen2.5:14b** as the default in this distribution

### Verification queries (v0.1.x)

- Q-A: `When can I run git push?` (simple fact retrieval)
- Q-B: `Why is KuzuDB used in this project?` (reasoning that requires background and rationale)

### num_ctx settings rationale

| Model | num_ctx | Rationale |
|---|---|---|
| llama3.1:8b | 65536 | 8B model weights 4.7GB + KV cache (FP16, 64K) ~4.0GB = 8.7GB total. Some offload on 8GB GPU but mostly GPU-resident. Plenty of headroom for Cognee cognify long-text processing |
| qwen2.5:14b | 8192 | Model weights 9GB are already in CPU offload. Increasing num_ctx slows it down further, so capped at 8K (sufficient for Cognee's short-prompt structured-output use case) |
| gemma4:e4b | 16384 | Model weights 5GB + KV cache 1GB = 6GB total, fully GPU-resident. Wider context favors Cognee cognify (long-document chunked processing) |

### v0.2.0 scope

In v0.2.0, only **qwen2.5:14b** has been re-verified across all features (8 tools) on Cognee 1.0.5 / Ladybug DB (35/40 ✅; see "Verification Results" near the top of this file). Re-verification of other LLMs on Ladybug DB has been left as future work.
