"""CLI commands for the Cognee Hermes memory plugin."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from .config import config_path, load_config


def _provider_active() -> bool:
    try:
        from hermes_cli.config import cfg_get
        from hermes_cli.config import load_config as load_hermes_config

        config = load_hermes_config()
        return cfg_get(config, "memory", "provider") == "cognee"
    except Exception:
        return False


def _installed_plugin_version() -> str:
    """The version of the plugin copy this code runs from (its plugin.yaml)."""
    manifest = Path(__file__).resolve().parents[1] / "plugin.yaml"
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    return ""


def _pip_package_version() -> str:
    """The pip-installed package version, if the package is in this env."""
    try:
        from importlib.metadata import version

        return version("cognee-integration-hermes-agent")
    except Exception:
        return ""


def _print_status(args) -> None:
    cfg = load_config()
    path = config_path()
    plugin_version = _installed_plugin_version()
    pip_version = _pip_package_version()
    print("\nCognee memory")
    print("-" * 40)
    print(f"  Active provider: {'yes' if _provider_active() else 'no'}")
    print(f"  Cognee package:  {'installed' if importlib.util.find_spec('cognee') else 'missing'}")
    print(f"  Plugin version:  {plugin_version or '(unknown)'}")
    print(f"  Mode:            {'remote' if cfg.get('service_url') else 'local'}")
    print(f"  Dataset:         {cfg.get('dataset')}")
    print(f"  Config:          {path or '(unknown)'}")
    print(f"  Service URL:     {cfg.get('service_url') or '(none)'}")
    print(f"  LLM key:         {'set' if cfg.get('llm_api_key') else 'missing'}")
    print(f"  API key:         {'set' if cfg.get('api_key') else 'missing'}")
    print(f"  Improve on end:  {cfg.get('improve_on_end')}")
    if pip_version and plugin_version and pip_version != plugin_version:
        # Hermes runs the copy under HERMES_HOME/plugins, so `pip install -U`
        # alone changes nothing until the installer refreshes that copy.
        print(
            f"  Update:          pip package is {pip_version} but this installed copy "
            f"is {plugin_version} — run `cognee-hermes-install` to update"
        )
    print()


def _print_config(args) -> None:
    cfg = dict(load_config())
    for key in ("llm_api_key", "api_key", "identity_password"):
        if cfg.get(key):
            cfg[key] = "***"
    print(json.dumps(cfg, indent=2, sort_keys=True))


def _run_setup(args) -> None:
    try:
        from hermes_cli.memory_setup import cmd_setup_provider

        cmd_setup_provider("cognee")
    except Exception as exc:
        print(f"Could not launch Hermes memory setup: {exc}")
        print("Run: hermes memory setup")


def _print_install(args) -> None:
    here = Path(__file__).resolve().parents[1]
    print("\nInstall via pip (recommended):")
    print("  pip install cognee-integration-hermes-agent")
    print("  cognee-hermes-install     # copies the plugin into $HERMES_HOME/plugins")
    print("  hermes memory setup")
    print("\nInstall as a local Hermes directory plugin (from a checkout):")
    print("  mkdir -p ~/.hermes/plugins/cognee")
    print(f"  cp -R {here}/. ~/.hermes/plugins/cognee/")
    print("  hermes memory setup\n")


def cognee_command(args) -> None:
    sub = getattr(args, "cognee_command", None)
    if sub == "setup":
        _run_setup(args)
    elif sub == "config":
        _print_config(args)
    elif sub == "install":
        _print_install(args)
    else:
        _print_status(args)


def register_cli(subparser) -> None:
    """Build the `hermes cognee` command tree."""
    subs = subparser.add_subparsers(dest="cognee_command")
    subs.add_parser("status", help="Show Cognee memory status")
    subs.add_parser("setup", help="Run Hermes memory setup for Cognee")
    subs.add_parser("config", help="Print Cognee plugin config with secrets redacted")
    subs.add_parser("install", help="Print installation commands")
    subparser.set_defaults(func=cognee_command)
