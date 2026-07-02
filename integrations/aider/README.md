# Cognee Graph Memory Integration for Aider

This module equips [Aider](https://github.com/Aider-AI) with a persistent, cross‑session memory layer powered by **Cognee**.  
It enables terminal‑based developers to store and retrieve project context, decisions, and logs across disconnected terminal sessions – even when using local models (Ollama) or any OpenAI‑compatible API.

---

## 🚀 Features

- **Native Aider tooling** – the integration exposes two async tools:
  - `add_project_memory(session_id, content)` – write memories to the graph.
  - `search_project_memory(session_id, query)` – retrieve relevant past context.
- **Provider‑agnostic** – works with OpenAI, Anthropic, Gemini, DeepSeek, or 100% local models via Ollama.
- **Session isolation** – each project (or session) maintains its own memory space.
- **Zero‑cost testing** – run the entire test suite against a local Ollama instance (no API keys needed).

---

## 📦 Installation

Clone the [cognee‑integrations](https://github.com/topoteretes/cognee-integrations) monorepo and install dependencies:

```bash
git clone https://github.com/jaya6400/cognee-integrations.git
cd cognee-integrations
uv sync
```

The integration is located at `integrations/aider/`. It will be automatically registered when you run `uv sync` from the root.

---

## ⚙️ Configuration

Copy the example environment file and tailor it to your backend:

```bash
cp integrations/aider/.env.example integrations/aider/.env
```

### Provider presets (choose one)

| **Provider** | `.env` settings |
|--------------|----------------|
| **Ollama (local, free)** | `LLM_PROVIDER=ollama`<br>`LLM_MODEL=llama3.2:1b` (or `llama3`)<br>`EMBEDDING_PROVIDER=ollama`<br>`EMBEDDING_MODEL=all-minilm` (or `nomic-embed-text`)<br>`EMBEDDING_DIMENSIONS=384` (768 for nomic)<br>`LLM_ENDPOINT=http://localhost:11434/v1`<br>`EMBEDDING_ENDPOINT=http://localhost:11434/api/embed`<br>`LLM_API_KEY=ollama` (dummy) |
| **OpenAI** | `LLM_PROVIDER=openai`<br>`LLM_MODEL=gpt-4o`<br>`EMBEDDING_PROVIDER=openai`<br>`EMBEDDING_MODEL=text-embedding-3-small`<br>`OPENAI_API_KEY=sk-...` |
| **Gemini** | `LLM_PROVIDER=gemini`<br>`LLM_MODEL=gemini-2.0-flash`<br>`EMBEDDING_PROVIDER=gemini`<br>`EMBEDDING_MODEL=text-embedding-004`<br>`GEMINI_API_KEY=...` |

> 💡 **Important**  
> - For Ollama, ensure the service is running (`ollama serve` or via Docker).  
> - Set `EMBEDDING_DIMENSIONS` to match your embedding model (384 for `all-minilm`, 768 for `nomic-embed-text`).  
> - For local testing, you may also add `ENABLE_BACKEND_ACCESS_CONTROL=false` and a dummy `COGNEE_USER_ID` to bypass permission checks.

---

## 🧪 Testing the Integration

Run the unit tests (they mock the Cognee client to avoid network calls):

```bash
cd integrations/aider
uv run --active pytest tests/ -v
```

To test the **full end‑to‑end flow** with your actual Ollama/OpenAI setup, run the example script:

```bash
uv run --active python examples/aider_memory_demo.py
```

This script will:
1. Start with a clean graph.
2. Add two sample memories about database choices.
3. Search for “What database should I connect to?” and print the results.

If it outputs the stored memories, your integration is ready.

---

## 🧑‍💻 Using with Aider (Experimental)

We provide a forward‑compatible adapter at `cognee_integration_aider.aider_adapter` that exposes sync wrappers for the tools.

### 🛠️ Setup

1. **Install Aider** (in a separate environment to avoid dependency conflicts):
   ```bash
   python -m venv aider_env
   source aider_env/bin/activate
   pip install aider-chat
   ```

2. **Install this integration** in the same environment (editable mode):
   ```bash
   cd /path/to/cognee-integrations
   pip install -e integrations/aider
   ```

3. **Set environment variables** – copy `.env.example` to `.env` and fill in your provider details (e.g., Ollama):
   ```bash
   cp integrations/aider/.env.example integrations/aider/.env
   # Edit .env with your LLM and embedding settings
   ```

4. **Export the environment** (or load via `source .env`):
   ```bash
   export $(grep -v '^#' integrations/aider/.env | xargs)
   ```

### 🧪 Current Workaround (demo script)

Because **Aider v0.86.2 does not yet support loading Python modules as custom tools**, the only way to test the integration right now is via the included demo script:

```bash
cd integrations/aider
uv run --active python examples/aider_memory_demo.py
```

This script:
- Adds two sample memories (database choices).
- Searches for “What database should I connect to?” and prints the results.

If it outputs the stored memories, your integration is correctly configured.

### ❌ What does **not** work (yet)

As shown in the screenshot below, running:

```bash
aider --load /path/to/aider_adapter.py
```

**fails** – because `--load` is for chat scripts, not Python modules.  
Aider tries to execute the Python code line by line as chat commands, resulting in `Invalid command` errors, and later triggers a known bug (`PermissionDeniedError` in litellm).

>![Attempt to load adapter with --load fails](https://github.com/user-attachments/assets/33710208-334e-4530-9ee0-e1dd4666bb56)

> ⚠️ **Note**: The `--load` flag in Aider is **not** for loading Python modules.  
> The `--tool` flag is ambiguous (matches `--tool-output-color` etc.) and not intended for this purpose.

### 🔮 Future usage (when Aider adds support)

Once Aider introduces a flag like `--tool-module` (or equivalent), you will be able to launch it with:

```bash
aider --tool-module cognee_integration_aider.aider_adapter
```

Then inside the chat, you can ask:

```
> Remember that we decided to use PostgreSQL with pgvector.
> What database did we decide to use?
```

Aider will call `add_project_memory` and `search_project_memory` via the adapter.

**Until then, use the demo script to verify the integration works.**

## 🧩 Project Structure

```
integrations/aider/
├── cognee_integration_aider/
│   ├── __init__.py
│   ├── tools.py          # async add/search functions
│   ├── config.py         # env‑aware configuration
│   └── aider_adapter.py  # adapter for Aider's tool format
├── tests/                # unit tests
├── examples/             # demo script
├── .env.example          # configuration template
├── pyproject.toml        # package metadata
└── README.md
```

---

## 🔧 Troubleshooting

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError: No module named 'transformers'` | Install `uv pip install transformers sentence-transformers` inside your environment. |
| `asyncio.run() cannot be called from a running event loop` | Your tools are sync but calling `asyncio.run()` – **use the async versions** provided in `tools.py` and call them with `await` from an async main. |
| `PermissionDeniedError` when adding/searching | Set `ENABLE_BACKEND_ACCESS_CONTROL=false` and `COGNEE_USER_ID=00000000-0000-0000-0000-000000000001` in your environment. |
| `LLM_API_KEY` missing | Even for Ollama, set `LLM_API_KEY=ollama` (dummy value) to satisfy validation. |
| Embedding endpoint timeout | Ensure `EMBEDDING_ENDPOINT` points to the correct URL (e.g., `http://localhost:11434/api/embed`) and that Ollama is running. |
| `ValueError: PermissionDeniedError is in litellm but not in aider's exceptions list` | This is a known bug in Aider 0.86.2. It is not related to this integration. Please report to the Aider team. |

---

## 🧑‍🤝‍🧑 Contributing

If you encounter issues or have ideas for improvements, please open an issue or PR in the [cognee‑integrations](https://github.com/topoteretes/cognee-integrations) repository.

---

**Happy coding with persistent memory!** 🧠
