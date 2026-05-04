# user_knowledge/

This folder is **where you place your own knowledge files (.md)**.

## How to use

1. Drop `.md` files into this folder (sub-folders by category are fine).
2. Run `src/venv/bin/python3 src/knowledge_src/split_knowledge.py`.
   - Each `.md` is split by H2 heading and written to `knowledge/user_chunks/`.
3. Run `src/venv/bin/python3 src/knowledge_src/import_knowledge.py`.
   - The split files under `user_chunks/` are ingested into Cognee.

## Recommended file format

- Markdown (`.md`)
- Use H1 (`# title`) to indicate the file's overall theme.
- Use H2 (`## section`) to delimit chapters (this is the split boundary).
- H3 and below: free-form.

## Why split

Cognee's cognify step (LLM-based summarization and graph construction) sometimes
**fails (status=errored) on large files (hundreds of lines)** because the LLM
cannot keep up. To avoid this, files are split by H2 heading before ingestion.

Each split file is prefixed with reference metadata pointing back to the
original file, so when knowledge is recalled from graph memory you can still
trace the source document.

## Notes

- This README.md is excluded from ingestion (auto-skipped at split and import).
- The folder layout is free-form, but flat per-category folders are preferred over deep nesting.
