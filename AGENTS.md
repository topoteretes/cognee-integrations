# AGENTS.md

This is the monorepo for Cognee-owned integration packages: each one, under `integrations/<name>/`, gives an agent framework or host a persistent memory layer backed by [cognee](https://github.com/topoteretes/cognee), and is developed and published independently (see `README.md`).

## Architecture checks (enola)

[enola](https://github.com/enola-labs/enola) builds a deterministic graph of this repository's structure and grades what a change does to it: layers crossed the wrong way, new cycles, coupling nobody asked for. The intended architecture is declared in `enola-intent.yaml` at the repository root.

The `Architecture (enola)` CI job reports what each pull request does to that architecture, as annotations and in the job summary. It is report-only: it fails only when `enola-intent.yaml` is invalid or one of its layers or components matches nothing.

**If you do not already use enola while developing, run it locally:**

- **Install** the version CI uses: `uv tool install enola-cli==0.5.4`.
- **Before changing shared code**, see what depends on it: `enola impact <symbol>`, or `enola plan --paths <files>` for the declared rules that govern those files.
- **Before opening a pull request**, grade the branch: `enola check --base origin/main`. It writes nothing. In a checkout whose directory is not named after the repository (a `git worktree`, for example), it declines to compare (exit 3); there, run `enola baseline pin` before you start editing and `enola check` when done.
- **With a coding agent**, serve the graph over MCP: `claude mcp add enola -- enola <path to this repository>`.
- **After editing `enola-intent.yaml`**, `enola --generate . && enola constraints lint --fail-empty .` must exit 0. Every rule needs a `because:` that says why it exists.

`enola-intent.yaml` lists, in its header, the documented rules it does not declare yet and why: enola 0.5.4 resolves `import cognee` to `integrations/dify/provider/cognee.py`, and calls between the host plugins' copied hook modules onto one of the copies, so for now treat cross-integration edges in an enola report with suspicion.

`.enola/` holds local snapshots and is gitignored.
