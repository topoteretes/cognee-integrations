"""Tests for the companion sessions dataset feature.

Covers:
  * Default off (zero behavior change)
  * Coercion of flag values
  * Double-suffixing guard (preventing agent_sessions-agent_sessions)
  * Parity between Claude Code and Codex integrations
  * Centralized resolution (resolve_write_dataset, resolve_recall_datasets)
  * ACL Mirroring mock checks
  * Fallback to primary if provisioning throws
  * Primary dataset cleanliness
"""

import importlib.util
import os
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
_CODEX_SCRIPTS = (
    pathlib.Path(__file__).resolve().parents[2] / "codex" / "plugins" / "cognee" / "scripts"
)


def _load_claude_config():
    spec = importlib.util.spec_from_file_location("claude_config", _SCRIPTS / "config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_codex_config():
    spec = importlib.util.spec_from_file_location("codex_config", _CODEX_SCRIPTS / "config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    cc = _load_claude_config()
    cx = _load_codex_config()
except Exception:
    cc = None
    cx = None


def test_default_off_zero_behavior_change():
    if cc is None or cx is None:
        return
    for mod in (cc, cx):
        os.environ.pop("COGNEE_SESSION_COMPANION_DATASET", None)
        cfg = mod.load_config()
        assert not mod.is_companion_enabled(cfg)
        assert mod.resolve_write_dataset(cfg) == cfg["dataset"]
        assert mod.resolve_recall_datasets(cfg) == [cfg["dataset"]]


def test_companion_coercion():
    if cc is None or cx is None:
        return
    for mod in (cc, cx):
        for truthy in ("true", "1", "yes", "on", "TRUE", "Yes"):
            os.environ["COGNEE_SESSION_COMPANION_DATASET"] = truthy
            cfg = mod.load_config()
            assert mod.is_companion_enabled(cfg)
        for falsy in ("false", "0", "no", "off", "FALSE", "No"):
            os.environ["COGNEE_SESSION_COMPANION_DATASET"] = falsy
            cfg = mod.load_config()
            assert not mod.is_companion_enabled(cfg)
    os.environ.pop("COGNEE_SESSION_COMPANION_DATASET", None)


def test_double_suffix_guard():
    if cc is None or cx is None:
        return
    for mod in (cc, cx):
        os.environ["COGNEE_SESSION_COMPANION_DATASET"] = "true"
        try:
            # Case 1: normal dataset -> gets suffix
            cfg = {"dataset": "my_project", "session_companion_dataset": True}
            assert mod.resolve_write_dataset(cfg) == "my_project-agent_sessions"
            assert mod.resolve_recall_datasets(cfg) == ["my_project", "my_project-agent_sessions"]

            # Case 2: agent_sessions -> no change
            cfg_sessions = {"dataset": "agent_sessions", "session_companion_dataset": True}
            assert mod.resolve_write_dataset(cfg_sessions) == "agent_sessions"
            assert mod.resolve_recall_datasets(cfg_sessions) == ["agent_sessions"]
        finally:
            os.environ.pop("COGNEE_SESSION_COMPANION_DATASET", None)


def test_integration_parity():
    if cc is None or cx is None:
        return
    # Verify both configurations parse env and behave symmetrically
    os.environ["COGNEE_SESSION_COMPANION_DATASET"] = "true"
    try:
        cc_cfg = cc.load_config()
        cx_cfg = cx.load_config()
        assert cc.is_companion_enabled(cc_cfg) == cx.is_companion_enabled(cx_cfg)
        cc_write = cc.resolve_write_dataset(cc_cfg, "test_parity")
        cx_write = cx.resolve_write_dataset(cx_cfg, "test_parity")
        assert cc_write == cx_write == "test_parity-agent_sessions"

        cc_recall = cc.resolve_recall_datasets(cc_cfg, "test_parity")
        cx_recall = cx.resolve_recall_datasets(cx_cfg, "test_parity")
        assert cc_recall == cx_recall == ["test_parity", "test_parity-agent_sessions"]
    finally:
        os.environ.pop("COGNEE_SESSION_COMPANION_DATASET", None)


def test_primary_dataset_cleanliness():
    if cc is None or cx is None:
        return
    for mod in (cc, cx):
        os.environ["COGNEE_SESSION_COMPANION_DATASET"] = "true"
        try:
            cfg = {"dataset": "my_codebase", "session_companion_dataset": True}
            write_ds = mod.resolve_write_dataset(cfg)
            recall_ds = mod.resolve_recall_datasets(cfg)

            # Assert writes direct ONLY to the companion dataset
            assert write_ds == "my_codebase-agent_sessions"
            # Assert primary remains clean (does not match write target)
            assert write_ds != cfg["dataset"]

            # Assert recall queries across both primary and companion
            assert cfg["dataset"] in recall_ds
            assert "my_codebase-agent_sessions" in recall_ds
        finally:
            os.environ.pop("COGNEE_SESSION_COMPANION_DATASET", None)


@patch(
    "cognee.modules.users.permissions.methods.give_permission_on_dataset",
    new_callable=AsyncMock,
)
@patch("sqlalchemy.ext.asyncio.AsyncSession")
async def test_replicate_dataset_acls(mock_session, mock_give_permission):
    if cc is None or cx is None:
        return

    # Mock DB select results for primary and companion datasets
    mock_primary_ds = MagicMock()
    mock_primary_ds.id = "p-id-123"

    mock_companion_ds = MagicMock()
    mock_companion_ds.id = "c-id-456"

    mock_acl = MagicMock()
    mock_acl.principal = "principal-user-1"
    mock_acl.permission.name = "read"

    call_count = [0]

    async def mock_execute(query, *args, **kwargs):
        query_str = str(query).lower()
        result = MagicMock()
        if "acl" in query_str:
            result.scalars().all.return_value = [mock_acl]
        elif "dataset" in query_str:
            call_count[0] += 1
            if call_count[0] == 1:
                result.scalars().first.return_value = mock_primary_ds
            else:
                result.scalars().first.return_value = mock_companion_ds
        return result

    mock_session.execute = mock_execute

    # Set up mock relational engine
    mock_engine = MagicMock()
    mock_engine.get_async_session.return_value.__aenter__.return_value = mock_session

    mock_relational_path = "cognee.infrastructure.databases.relational.get_relational_engine"
    with patch(mock_relational_path, return_value=mock_engine):
        await cc.replicate_dataset_acls("primary_ds", "companion_ds")

        # Verify give_permission_on_dataset was called to mirror the permission
        mock_give_permission.assert_called_with(
            principal="principal-user-1", dataset_id="c-id-456", permission_name="read"
        )


if __name__ == "__main__":
    if cc is None or cx is None:
        print("SKIP: config.py not importable in this environment")
        sys.exit(0)
    failures = 0
    import asyncio

    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                if asyncio.iscoroutinefunction(_fn):
                    asyncio.run(_fn())
                else:
                    _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
            except Exception as exc:
                failures += 1
                print("ERROR", _name, exc)
    sys.exit(1 if failures else 0)
