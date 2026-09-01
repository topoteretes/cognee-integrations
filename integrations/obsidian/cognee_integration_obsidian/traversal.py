import asyncio
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path

import cognee
import yaml

VAULT_PATH: str = os.environ.get("VAULT", "")
if not VAULT_PATH:
    raise SystemExit(
        "VAULT environment variable is required — set it to the path of your Obsidian vault, "
        'e.g. export VAULT="/path/to/your/vault"'
    )
EXCLUDED_DIRS = {".obsidian", ".trash", "Templates"}
dataset_name = Path(VAULT_PATH).name

# Namespaced per-vault so running against multiple vaults from the same
# directory doesn't collide, and overridable if you want it somewhere else.
MANIFEST_PATH = Path(os.environ.get("MANIFEST_PATH", f".cognee-manifest-{dataset_name}.json"))

# Where the graph visualization gets written; configurable instead of always cwd.
GRAPH_OUTPUT_PATH = os.environ.get("GRAPH_OUTPUT", "graph.html")


def load_manifest():
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_manifest(manifest):
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=4))


def split_frontmatter(text):
    frontmatter_pattern = r"^---\n(.*?)\n---\n"
    match = re.match(frontmatter_pattern, text, re.DOTALL)
    if match:
        frontmatter = match.group(1)
        content = text[match.end() :]
        try:
            frontmatter_data = yaml.safe_load(frontmatter) if frontmatter else {}
        except yaml.YAMLError:
            frontmatter_data = {}
        if not isinstance(frontmatter_data, dict):
            frontmatter_data = {}
        return frontmatter_data, content
    else:
        return None, text


def clean_link_target(raw):
    target = raw.split("|", 1)[0]
    target = target.split("#", 1)[0]
    target = target.split("^", 1)[0]
    return target


async def main():
    manifest = load_manifest()
    found_keys = set()
    any_ingested = False

    for root, dirs, files in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
        for filename in files:
            if not filename.endswith(".md"):
                continue

            full_path = os.path.join(root, filename)
            key = str(Path(full_path).relative_to(VAULT_PATH))
            found_keys.add(key)

            try:
                text = Path(full_path).read_text(encoding="utf-8")
                frontmatter, content = split_frontmatter(text)
                headings = re.findall(r"^(#{1,6})\s+(.*)", content, re.MULTILINE)
                links = Counter(
                    clean_link_target(link) for link in re.findall(r"\[\[([^\]]+)\]\]", text)
                )
                tags = [t for t in re.findall(r"(?<!\w)#([\w/-]+)", content) if not t.isdigit()]

                frontmatter_tags = (frontmatter or {}).get("tags", []) or []
                if isinstance(frontmatter_tags, str):
                    frontmatter_tags = [frontmatter_tags]
                node_set = list(set(frontmatter_tags) | set(tags))

                header_lines = [f"{k}: {v}" for k, v in (frontmatter or {}).items() if k != "tags"]
                header = "\n".join(header_lines)
                data = f"{header}\n\n{content}" if header else content

                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

                if manifest.get(key) == digest:
                    print("SKIP (unchanged):", key)
                else:
                    print("INGEST:", key)
                    await cognee.add(data=data, dataset_name=dataset_name, node_set=node_set)
                    manifest[key] = digest
                    any_ingested = True
                    save_manifest(manifest)

                print("Headings:", headings)
                print("Links:", links)
                print("Tags:", tags)
                print("Word count:", len(text.split()))
                print("..........")
                print(full_path)
                print("==========")
                print("Frontmatter:", frontmatter)
                print("Key:", key)

            except Exception as e:
                print(
                    f"ERROR processing {key}: {e} — "
                    f"skipping this note, continuing with the rest of the vault."
                )
                continue

    deleted_keys = set(manifest.keys()) - found_keys
    if deleted_keys:
        print(
            f"Pruning {len(deleted_keys)} manifest "
            f"entr{'y' if len(deleted_keys) == 1 else 'ies'} "
            f"for notes no longer found in the vault:"
        )
        for k in deleted_keys:
            print("  -", k)
            del manifest[k]
        save_manifest(manifest)

    if any_ingested:
        await cognee.cognify(datasets=[dataset_name])
        await cognee.visualize_graph(
            destination_file_path=os.path.abspath(GRAPH_OUTPUT_PATH),
            dataset=dataset_name,
            full=True,
        )
    else:
        print("No new or changed notes this run — skipping cognify and graph visualization.")


def cli():
    """Sync entry point for [project.scripts] — asyncio.run() needs a sync caller."""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
