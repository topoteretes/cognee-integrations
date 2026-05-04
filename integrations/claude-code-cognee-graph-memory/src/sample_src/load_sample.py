"""
Load bundled sample knowledge into Cognee graph memory.

For post-setup verification. This is a thin wrapper that internally calls
`src/main_src/import_to_graph.py --target sample`.

Usage:
    cd <distribution root directory>
    src/venv/bin/python3 src/sample_src/load_sample.py
"""
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # src/sample_src -> src -> distribution root
IMPORT_SCRIPT = PROJECT_ROOT / "src" / "main_src" / "import_to_graph.py"
PYTHON = PROJECT_ROOT / "src" / "venv" / "bin" / "python3"


def main() -> None:
    """Invoke src/main_src/import_to_graph.py --target sample"""
    if not IMPORT_SCRIPT.exists():
        print(f"Error: {IMPORT_SCRIPT} not found", file=sys.stderr)
        sys.exit(1)

    if not PYTHON.exists():
        print(f"Error: venv Python not found: {PYTHON}", file=sys.stderr)
        print("Please create the venv following docs/SETUP.md", file=sys.stderr)
        sys.exit(1)

    # Delegate to import_to_graph.py
    result = subprocess.run(
        [str(PYTHON), str(IMPORT_SCRIPT), "--target", "sample"],
        cwd=str(PROJECT_ROOT),
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
