# user_chunks/

This folder receives the **intermediate files split by H2 heading** from
`user_knowledge/`.

## Auto-generated content

- Running `src/knowledge_src/split_knowledge.py` generates the split files here.
- `src/knowledge_src/import_knowledge.py` ingests the files here into Cognee.
- You **do not need to edit** anything here (auto-generated, auto-cleared).

## Do not place files here manually

Each run of `split_knowledge.py` clears this folder's contents (except
README.md) and regenerates them. Place your source files under
`user_knowledge/` instead.

## Why this folder exists

Cognee's cognify step can fail on large files, so splitting is required before
ingestion. Separating the source folder (`user_knowledge/`) from the ingestion
folder (`user_chunks/`) keeps editing and ingestion concerns apart.
