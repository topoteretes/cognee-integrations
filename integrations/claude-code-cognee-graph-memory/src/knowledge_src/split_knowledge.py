"""
Split knowledge files (.md) by H2 heading.

Cognee's cognify step occasionally fails to summarize large files (hundreds of
lines) because the LLM cannot keep up. To avoid this, split each file by H2
(`## heading`) into smaller chunks before ingesting. Each chunk is prefixed
with reference metadata pointing back to the original file.

Usage:
    cd <distribution root directory>
    src/venv/bin/python3 src/knowledge_src/split_knowledge.py

Input:  .md files under knowledge/user_knowledge/
Output: split files under knowledge/user_chunks/
"""
import logging
import re
import shutil
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
INPUT_DIR = PROJECT_ROOT / "knowledge" / "user_knowledge"
OUTPUT_DIR = PROJECT_ROOT / "knowledge" / "user_chunks"


def sanitize_filename(name: str) -> str:
    """Remove or replace characters that are illegal in filenames."""
    name = re.sub(r"[/\\:*?\"<>|]", "", name)
    name = re.sub(r"\s+", "_", name.strip())
    if len(name) > 60:
        name = name[:60]
    return name


def split_md_by_h2(content: str) -> tuple[list[tuple[str, str]], str]:
    """Split markdown by H2 heading.

    Returns:
        (sections, h1_title)
        sections: [(section title, section body), ...]
        h1_title: text of the H1 heading (empty if none)
    """
    lines = content.split("\n")
    sections: list[tuple[str, str]] = []

    h1_title = ""
    i = 0
    # Skip the preamble before H1 / H2
    while i < len(lines):
        line = lines[i]
        if line.startswith("# "):
            h1_title = line[2:].strip()
            i += 1
            continue
        if line.startswith("## "):
            break
        i += 1

    # Split per H2 heading
    current_section_title = ""
    current_lines: list[str] = []

    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            if current_section_title:
                sections.append((current_section_title, "\n".join(current_lines).strip()))
            current_section_title = line[3:].strip()
            current_lines = [line]
        else:
            current_lines.append(line)
        i += 1

    if current_section_title:
        sections.append((current_section_title, "\n".join(current_lines).strip()))

    return sections, h1_title


def process_file(md_path: Path) -> int:
    """Split a single file and write the chunks under user_chunks/. Returns the chunk count."""
    rel = md_path.relative_to(INPUT_DIR)
    content = md_path.read_text(encoding="utf-8")
    sections, h1_title = split_md_by_h2(content)

    out_dir = OUTPUT_DIR / rel.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Files with no H2 are emitted as a single chunk
    if not sections:
        out_path = out_dir / (md_path.stem + "_00_full.md")
        out_path.write_text(content, encoding="utf-8")
        return 1

    base_name = md_path.stem
    chunk_count = 0
    for idx, (section_title, section_body) in enumerate(sections, 1):
        # Prepend reference metadata pointing back to the source file
        header = (
            f"# {h1_title} - {section_title}\n\n"
            f"> This file is the section `{section_title}` extracted from `user_knowledge/{rel}`.\n"
            f"> Source document: `user_knowledge/{rel}`\n\n"
        )
        chunk_content = header + section_body

        section_clean = sanitize_filename(section_title)
        chunk_filename = f"{base_name}_{idx:02d}_{section_clean}.md"
        out_path = out_dir / chunk_filename
        out_path.write_text(chunk_content, encoding="utf-8")
        chunk_count += 1

    return chunk_count


def main() -> None:
    """Entry point."""
    if not INPUT_DIR.exists():
        logger.error("Input folder not found: %s", INPUT_DIR)
        sys.exit(1)

    md_files = sorted(INPUT_DIR.glob("**/*.md"))
    # README.md is excluded (folder description, not knowledge content)
    md_files = [f for f in md_files if f.name != "README.md"]

    if not md_files:
        logger.warning("No .md files to split under: %s", INPUT_DIR)
        logger.warning("Place your knowledge files under user_knowledge/ and rerun")
        sys.exit(1)

    # Clear any existing chunks before regenerating
    if OUTPUT_DIR.exists():
        # Keep README.md
        for item in OUTPUT_DIR.iterdir():
            if item.name == "README.md":
                continue
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        logger.info("Cleared existing chunks: %s", OUTPUT_DIR)
    else:
        OUTPUT_DIR.mkdir(parents=True)

    logger.info("Files to split: %d", len(md_files))
    total_chunks = 0
    for md_path in md_files:
        rel = md_path.relative_to(INPUT_DIR)
        n = process_file(md_path)
        total_chunks += n
        logger.info("  %s: %d chunks", rel, n)

    logger.info("Split complete: %d chunks generated", total_chunks)
    logger.info("Output: %s", OUTPUT_DIR)


if __name__ == "__main__":
    main()
