"""Materialize the Hermes directory plugin from the installed pip package.

Hermes discovers memory providers by scanning ``$HERMES_HOME/plugins/`` — a pip
install alone puts this package in site-packages, where Hermes never looks. The
``cognee-hermes-install`` console script bridges that gap: it lays down the
exact directory shape the scanner expects (``plugin.yaml`` and the ``cli.py``
shim at the plugin root, this package beside them), copied from the installed
wheel. The full flow:

    pip install cognee-integration-hermes-agent
    cognee-hermes-install
    hermes memory setup

Because Hermes runs the *copy*, ``pip install -U`` alone changes nothing —
re-run ``cognee-hermes-install`` after upgrading. ``hermes cognee status``
warns when the pip package is newer than the installed copy.

The plugin-root files ship inside the package under ``_plugin_root/`` (a wheel
cannot carry files outside its packages); a test pins them byte-identical to
the repository's canonical copies.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from .config import resolve_hermes_home

_ROOT_FILES = ("plugin.yaml", "cli.py", "after-install.md")


def install(hermes_home: str | Path | None = None) -> Path:
    """Copy the plugin into ``$HERMES_HOME/plugins/cognee``; return that path."""
    home = resolve_hermes_home(hermes_home)
    if home is None:
        # No Hermes on the path to ask; the documented default.
        home = Path.home() / ".hermes"
    package_src = Path(__file__).resolve().parent
    root_src = package_src / "_plugin_root"
    missing = [name for name in _ROOT_FILES if not (root_src / name).is_file()]
    if missing:
        raise RuntimeError(
            "packaged plugin files missing (%s) — this looks like a broken or "
            "source-tree install; reinstall the package from PyPI" % ", ".join(missing)
        )

    target = home / "plugins" / "cognee"
    target.mkdir(parents=True, exist_ok=True)
    for name in _ROOT_FILES:
        shutil.copyfile(root_src / name, target / name)

    # Replace the package wholesale so removed modules never linger.
    dest_package = target / package_src.name
    if dest_package.exists():
        shutil.rmtree(dest_package)
    shutil.copytree(
        package_src,
        dest_package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cognee-hermes-install",
        description="Install the Cognee memory plugin into a Hermes Agent home.",
    )
    parser.add_argument(
        "--home",
        default=None,
        help="HERMES_HOME to install into (default: auto-detect, else ~/.hermes)",
    )
    args = parser.parse_args(argv)
    target = install(args.home)
    print(f"Cognee memory plugin installed at {target}")
    print("Next: run `hermes memory setup` and select `cognee`.")
    print("After a `pip install -U`, re-run `cognee-hermes-install` to update this copy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
