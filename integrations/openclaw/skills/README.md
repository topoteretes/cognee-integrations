# OpenClaw × Cognee setup skills

A hub of **self-contained setup skills** for running the OpenClaw ↔ Cognee
integration in various configurations. Each subdirectory holds one skill: a
single `SKILL.md` that an agent (or a person) can read and execute end-to-end to
stand up a working deployment — generate the files, run the services, verify it,
and wire up the OpenClaw plugin.

The goal is discoverability: point an LLM at this repo and these skills are easy
to find and follow. Skills here are **generators** — they embed the exact file
contents to create (Dockerfiles, compose files, config), so the agent writes and
runs them rather than improvising.

## Available skills

| Skill | Directory | What it sets up |
|-------|-----------|-----------------|
| `cognee-falkor-setup` | [`falkor/`](./falkor/SKILL.md) | Cognee with **FalkorDB** as the vector + graph store (custom image + adapter), running alongside FalkorDB, with per-agent graphs. |

_More setups will be added here over time (e.g. other vector/graph backends,
cloud deployments, alternative LLM providers)._

## How to use a skill

1. Browse the table above (or list the subdirectories) and open the relevant `SKILL.md`.
2. Follow it top to bottom. Each skill states its **preconditions**, the **files to generate**, the **commands to run**, and **verification checks** with "if it fails" guidance.
3. Don't skip the verification steps — they're what make the skill reliable rather than hopeful.

For agents: each `SKILL.md` has YAML frontmatter (`name`, `description`). Match a
task to a skill by its `description`, then execute the body. Treat the fenced
file blocks as exact content to write verbatim.

## Conventions for adding a new skill

Keep this hub consistent so skills stay easy to find and trust:

- **One directory per skill**, named for the thing it sets up (e.g. `falkor/`, `neo4j/`, `cloud/`).
- **A single `SKILL.md`** as the entry point, with frontmatter:
  ```yaml
  ---
  name: <kebab-case-skill-name>
  description: Use when <trigger>. <One-line summary of what it stands up and that it is self-contained.>
  ---
  ```
- **Self-contained & generator-style**: embed exact file contents in fenced code
  blocks; don't describe fragile files in prose (agents free-handing an entrypoint
  or Dockerfile is the main failure mode).
- **Self-verifying**: after each major step, include a concrete check (a `curl`,
  a log grep, an in-container assertion) and a short "if it fails, do X" note so
  the agent self-corrects instead of shipping something silently broken.
- **State preconditions up front** (can write files? run Docker? has API keys?) so
  the skill fails loudly when it can't proceed.
- **Pin versions** (base images, packages) with a note on how to bump them.
- **Add a row to the table above** when you create a new skill.
