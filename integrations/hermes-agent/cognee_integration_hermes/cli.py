"""CLI commands for the Cognee Hermes memory plugin."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

from . import code_graph, update_check
from .config import DEFAULT_LOCAL_PORT, config_path, load_config, resolve_local_roots


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


def _update_hint(cfg, *, force: bool = False) -> str:
    """A one-line nudge when PyPI has a newer release, else "". Never raises."""
    if not cfg.get("update_check", True):
        return ""
    pip_version = _pip_package_version()
    if not pip_version:
        return ""
    latest = update_check.latest_published_version(
        interval=float(cfg.get("update_check_interval") or 3600), force=force
    )
    if not update_check.is_newer(latest, pip_version):
        return ""
    return (
        f"update available: {pip_version} → {latest} — run "
        f"`pip install -U {update_check.PYPI_PACKAGE}` then `cognee-hermes-install`"
    )


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
    else:
        hint = _update_hint(cfg, force=getattr(args, "check_updates", False))
        if hint:
            print(f"  Update:          {hint}")
    print()


def _print_version(args) -> None:
    cfg = load_config()
    plugin_version = _installed_plugin_version()
    pip_version = _pip_package_version()
    print(f"cognee-integration-hermes-agent {pip_version or plugin_version or '(unknown)'}")
    if plugin_version and pip_version and plugin_version != pip_version:
        print(
            f"  installed plugin copy is {plugin_version} — run `cognee-hermes-install` "
            "to refresh it from the pip package"
        )
    hint = _update_hint(cfg, force=getattr(args, "check_updates", False))
    if hint:
        print(f"  {hint}")


def _connect_backend():
    """A connected HttpBackend for one-shot CLI operations.

    Mirrors the provider's connection modes minus embedded (these operations
    are HTTP-only): a set service_url is used as-is, otherwise the shared
    local server is ensured — booted if nobody has yet — exactly as a session
    would. Registration keeps the server's idle watchdog honest for the
    duration; the caller must close().
    """
    from .http_backend import HttpBackend
    from .server_bootstrap import ensure_local_server

    cfg = load_config()
    backend = HttpBackend(agent_session_name="hermes-cli")
    service_url = str(cfg.get("service_url") or "")
    if service_url:
        backend.connect(url=service_url, api_key=str(cfg.get("api_key") or ""), timeout=30)
        return backend
    data_root, system_root = resolve_local_roots(cfg)
    local_url = ensure_local_server(
        int(cfg.get("local_port") or DEFAULT_LOCAL_PORT),
        data_root=data_root,
        system_root=system_root,
        boot_timeout=float(cfg.get("server_boot_timeout") or 600),
    )
    backend.connect(url=local_url, api_key="", timeout=30)
    return backend


def _run_index_repo(args) -> int:
    """Index one repository into a deterministic code graph (cognee >= 1.5.3)."""
    spec = code_graph.canonical_spec(str(args.repo))
    if not code_graph.is_remote_repo(spec) and not Path(spec).is_dir():
        print(f"Error: {args.repo!r} is not a directory or a recognized git URL.")
        return 1
    dataset = str(getattr(args, "dataset", "") or "") or code_graph.default_code_dataset(spec)
    index_vectors = bool(getattr(args, "index_vectors", False))
    wait_seconds = float(getattr(args, "wait", 0.0) or 0.0)

    try:
        backend = _connect_backend()
    except Exception as exc:
        print(f"Error: could not reach a cognee server: {exc}")
        return 1
    try:
        result = backend.index_repository(
            repo=spec, dataset=dataset, index_vectors=index_vectors, timeout=120
        )
        status = str(result.get("status") or "submitted")
        print(f"Indexing {spec}")
        print(f"  dataset: {dataset}")
        print(f"  status:  {status}")
        dataset_id = str(result.get("dataset_id") or "")
        if wait_seconds > 0 and dataset_id:
            outcome = _poll_code_graph(backend, dataset_id, wait_seconds)
            print(f"  outcome: {outcome}")
            status = outcome
        elif wait_seconds > 0:
            print("  outcome: unknown (the server returned no dataset_id to poll)")
        code_graph.record_index(spec, dataset, index_vectors=index_vectors, status=status)
        print(
            "Registered for code recall. Query it with the cognee_code_search tool "
            "in a Hermes session."
        )
        return 0
    except Exception as exc:
        print(f"Error: indexing failed: {exc}")
        return 1
    finally:
        try:
            backend.close(timeout=5)
        except Exception:
            pass


def _poll_code_graph(backend, dataset_id: str, deadline_seconds: float) -> str:
    """Poll the code_graph_pipeline until completed/errored/timeout."""
    deadline = time.monotonic() + max(0.0, deadline_seconds)
    while True:
        status = ""
        try:
            status = backend.dataset_pipeline_status(dataset_id=dataset_id, timeout=10)
        except Exception:
            pass
        if status.endswith("COMPLETED"):
            return "completed"
        if status.endswith("ERRORED"):
            return "errored"
        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(3.0)


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
    elif sub == "version":
        _print_version(args)
    elif sub == "index-repo":
        raise SystemExit(_run_index_repo(args))
    else:
        _print_status(args)


def register_cli(subparser) -> None:
    """Build the `hermes cognee` command tree."""
    subs = subparser.add_subparsers(dest="cognee_command")
    status = subs.add_parser("status", help="Show Cognee memory status")
    status.add_argument(
        "--check-updates", action="store_true", help="Force a live PyPI update check"
    )
    subs.add_parser("setup", help="Run Hermes memory setup for Cognee")
    subs.add_parser("config", help="Print Cognee plugin config with secrets redacted")
    subs.add_parser("install", help="Print installation commands")
    version = subs.add_parser("version", help="Show plugin version and update availability")
    version.add_argument(
        "--check-updates", action="store_true", help="Force a live PyPI update check"
    )
    index = subs.add_parser(
        "index-repo",
        help="Index a repository into a deterministic Cognee code graph (cognee >= 1.5.3)",
    )
    index.add_argument("repo", help="Local path or git URL of the repository")
    index.add_argument(
        "--dataset", default="", help="Dataset name (default: codebase-<repo>-<digest>)"
    )
    index.add_argument(
        "--index-vectors",
        action="store_true",
        help="Also embed code entities for semantic search (needs an embedding provider)",
    )
    index.add_argument(
        "--wait",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help="Wait up to SECONDS for the indexing pipeline to finish",
    )
    subparser.set_defaults(func=cognee_command)
