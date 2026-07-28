"""Characterization: config resolution, coercion, and the files we write.

The subtle one is precedence: ``cognee.json`` is applied *over* the environment,
so a saved config wins. Inverting that on rewrite would silently break every
documented env var for anyone who has run the setup wizard.

Run standalone with ``python3 tests/test_config_contract.py``.
"""

import contextlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from _char_helpers import make_provider  # noqa: E402
from cognee_integration_hermes import config as config_mod  # noqa: E402

# Every env var load_config reads. Removed (not blanked) before each test:
# some keys distinguish "absent" from "empty" — see TestEmptyEnvVarQuirks.
_MANAGED_KEYS = (
    "COGNEE_BASE_URL",
    "COGNEE_SERVICE_URL",
    "COGNEE_API_KEY",
    "COGNEE_EMBEDDED",
    "COGNEE_DATASET",
    "COGNEE_TOP_K",
    "COGNEE_LOCAL_PORT",
    "COGNEE_SERVER_BOOT_TIMEOUT",
    "COGNEE_AUTO_ROUTE",
    "COGNEE_IMPROVE_ON_END",
    "COGNEE_IMPROVE_BACKGROUND",
    "COGNEE_SESSION_PREFIX",
    "COGNEE_DATA_ROOT",
    "COGNEE_SYSTEM_ROOT",
    "COGNEE_RECALL_TIMEOUT",
    "COGNEE_WRITE_TIMEOUT",
    "COGNEE_IMPROVE_TIMEOUT",
    "COGNEE_HERMES_USER_EMAIL",
    "COGNEE_HERMES_USER_PASSWORD",
    "LLM_API_KEY",
    "LLM_MODEL",
)


@contextlib.contextmanager
def _clean_env(overrides=None):
    """Run with every managed key *removed*, then apply *overrides*."""
    env = {key: value for key, value in os.environ.items() if key not in _MANAGED_KEYS}
    env.update(overrides or {})
    with mock.patch.dict(os.environ, env, clear=True):
        yield


class _TmpHome:
    """A temporary HERMES_HOME with an optional pre-written cognee.json."""

    def __init__(self, file_config=None):
        self._file_config = file_config
        self._tmp = None

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        home = Path(self._tmp.name)
        if self._file_config is not None:
            (home / "cognee.json").write_text(json.dumps(self._file_config), encoding="utf-8")
        return home

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False


def _load(env=None, file_config=None):
    with _TmpHome(file_config) as home:
        with _clean_env(env):
            return config_mod.load_config(home)


# --------------------------------------------------------------------------
# Precedence
# --------------------------------------------------------------------------


class TestPrecedence(unittest.TestCase):
    def test_file_config_overrides_the_environment(self):
        cfg = _load(
            env={"COGNEE_DATASET": "from_env"},
            file_config={"dataset": "from_file"},
        )
        self.assertEqual(cfg["dataset"], "from_file")

    def test_environment_is_used_when_the_file_omits_the_key(self):
        cfg = _load(env={"COGNEE_DATASET": "from_env"}, file_config={"top_k": 3})
        self.assertEqual(cfg["dataset"], "from_env")

    def test_null_file_values_do_not_override(self):
        cfg = _load(env={"COGNEE_DATASET": "from_env"}, file_config={"dataset": None})
        self.assertEqual(cfg["dataset"], "from_env")

    def test_unreadable_file_is_ignored_rather_than_fatal(self):
        with _TmpHome() as home:
            (home / "cognee.json").write_text("{not json", encoding="utf-8")
            with _clean_env():
                cfg = config_mod.load_config(home)
        self.assertEqual(cfg["dataset"], config_mod.DEFAULT_DATASET)

    def test_base_url_wins_over_the_deprecated_alias(self):
        cfg = _load(
            env={"COGNEE_BASE_URL": "https://canonical", "COGNEE_SERVICE_URL": "https://legacy"}
        )
        self.assertEqual(cfg["service_url"], "https://canonical")


# --------------------------------------------------------------------------
# Defaults and coercion
# --------------------------------------------------------------------------


class TestDefaults(unittest.TestCase):
    def test_documented_defaults(self):
        cfg = _load()
        self.assertEqual(cfg["dataset"], "hermes")
        self.assertEqual(cfg["top_k"], 5)
        self.assertEqual(cfg["local_port"], 8000)
        self.assertEqual(cfg["server_boot_timeout"], 30)
        self.assertEqual(cfg["session_prefix"], "hermes")
        self.assertEqual(cfg["recall_timeout"], 120)
        self.assertEqual(cfg["write_timeout"], 120)
        self.assertEqual(cfg["improve_timeout"], 300)
        self.assertIs(cfg["auto_route"], True)
        self.assertIs(cfg["improve_on_end"], True)
        self.assertIs(cfg["embedded"], False)

    def test_improve_background_is_an_empty_tristate_by_default(self):
        # "" means auto (background in server/remote, synchronous in embedded).
        self.assertEqual(_load()["improve_background"], "")

    def test_improve_background_can_be_forced(self):
        self.assertEqual(
            _load(env={"COGNEE_IMPROVE_BACKGROUND": "false"})["improve_background"], "false"
        )


class TestCoercion(unittest.TestCase):
    def test_top_k_floor_is_one(self):
        self.assertEqual(_load(file_config={"top_k": 0})["top_k"], 1)
        self.assertEqual(_load(file_config={"top_k": -5})["top_k"], 1)

    def test_non_numeric_top_k_falls_back_to_the_default(self):
        self.assertEqual(_load(file_config={"top_k": "abc"})["top_k"], 5)

    def test_local_port_clamped_to_the_valid_range(self):
        self.assertEqual(_load(env={"COGNEE_LOCAL_PORT": "999999"})["local_port"], 65535)
        self.assertEqual(_load(env={"COGNEE_LOCAL_PORT": "0"})["local_port"], 1)

    def test_timeout_floors_are_one(self):
        cfg = _load(file_config={"recall_timeout": 0, "write_timeout": -1, "improve_timeout": 0})
        self.assertEqual(cfg["recall_timeout"], 1)
        self.assertEqual(cfg["write_timeout"], 1)
        self.assertEqual(cfg["improve_timeout"], 1)

    def test_server_boot_timeout_floor_is_one(self):
        self.assertEqual(_load(file_config={"server_boot_timeout": 0})["server_boot_timeout"], 1)

    def test_string_booleans_from_the_config_file_are_coerced(self):
        cfg = _load(file_config={"auto_route": "no", "improve_on_end": "0", "embedded": "yes"})
        self.assertIs(cfg["auto_route"], False)
        self.assertIs(cfg["improve_on_end"], False)
        self.assertIs(cfg["embedded"], True)


class TestEmptyEnvVarQuirks(unittest.TestCase):
    """An empty env var is not the same as an absent one — and not uniformly so.

    Plain ``os.environ.get(key, DEFAULT)`` reads take the empty string as a real
    value, while the ``str_to_int`` / ``str_to_bool`` reads fall back to their
    default. Pinned because it is exactly the kind of asymmetry a rewrite tidies
    up by accident, changing behaviour for anyone who exports a blank var.
    """

    def test_blank_dataset_is_honoured_as_empty(self):
        self.assertEqual(_load(env={"COGNEE_DATASET": ""})["dataset"], "")

    def test_blank_session_prefix_is_honoured_as_empty(self):
        self.assertEqual(_load(env={"COGNEE_SESSION_PREFIX": ""})["session_prefix"], "")

    def test_blank_numeric_vars_fall_back_to_defaults(self):
        cfg = _load(env={"COGNEE_TOP_K": "", "COGNEE_LOCAL_PORT": ""})
        self.assertEqual(cfg["top_k"], 5)
        self.assertEqual(cfg["local_port"], 8000)

    def test_blank_boolean_vars_fall_back_to_defaults(self):
        cfg = _load(env={"COGNEE_AUTO_ROUTE": "", "COGNEE_EMBEDDED": ""})
        self.assertIs(cfg["auto_route"], True)
        self.assertIs(cfg["embedded"], False)

    def test_the_provider_re_defaults_a_blank_dataset_at_use_time(self):
        # provider.initialize does `config.get("dataset") or DEFAULT_DATASET`, so a
        # blank dataset never reaches cognee — the recovery lives there, not in
        # load_config. Both halves have to survive together.
        provider = make_provider()
        self.assertEqual(
            str(_load(env={"COGNEE_DATASET": ""}).get("dataset") or "hermes"),
            provider._dataset,
        )


class TestStrToBool(unittest.TestCase):
    def test_truthy_spellings(self):
        for value in ("1", "true", "TRUE", "yes", "y", "on", " True "):
            self.assertTrue(config_mod.str_to_bool(value, False), value)

    def test_falsy_spellings(self):
        for value in ("0", "false", "no", "n", "off", " Off "):
            self.assertFalse(config_mod.str_to_bool(value, True), value)

    def test_unrecognized_and_none_use_the_default(self):
        self.assertTrue(config_mod.str_to_bool("maybe", True))
        self.assertFalse(config_mod.str_to_bool("maybe", False))
        self.assertTrue(config_mod.str_to_bool(None, True))

    def test_actual_booleans_pass_through(self):
        self.assertTrue(config_mod.str_to_bool(True, False))
        self.assertFalse(config_mod.str_to_bool(False, True))


# --------------------------------------------------------------------------
# Files we write
# --------------------------------------------------------------------------


class TestWriteEnvVars(unittest.TestCase):
    def test_creates_the_file_with_owner_only_permissions(self):
        with _TmpHome() as home:
            env_path = home / ".env"
            config_mod.write_env_vars(env_path, {"COGNEE_API_KEY": "secret"})
            mode = stat.S_IMODE(env_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_updates_an_existing_key_in_place(self):
        with _TmpHome() as home:
            env_path = home / ".env"
            env_path.write_text("COGNEE_API_KEY=old\n", encoding="utf-8")
            config_mod.write_env_vars(env_path, {"COGNEE_API_KEY": "new"})
            lines = env_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["COGNEE_API_KEY=new"])

    def test_preserves_unrelated_lines_and_comments(self):
        with _TmpHome() as home:
            env_path = home / ".env"
            env_path.write_text(
                "# a comment\nOTHER_TOOL=keepme\nCOGNEE_API_KEY=old\n", encoding="utf-8"
            )
            config_mod.write_env_vars(env_path, {"COGNEE_API_KEY": "new"})
            content = env_path.read_text(encoding="utf-8")
        self.assertIn("# a comment", content)
        self.assertIn("OTHER_TOOL=keepme", content)
        self.assertIn("COGNEE_API_KEY=new", content)
        self.assertNotIn("COGNEE_API_KEY=old", content)

    def test_appends_keys_that_are_not_present(self):
        with _TmpHome() as home:
            env_path = home / ".env"
            env_path.write_text("EXISTING=1\n", encoding="utf-8")
            config_mod.write_env_vars(env_path, {"NEW_KEY": "2"})
            lines = env_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["EXISTING=1", "NEW_KEY=2"])

    def test_empty_value_clears_a_key(self):
        # This is how post_setup retires the deprecated COGNEE_SERVICE_URL alias.
        with _TmpHome() as home:
            env_path = home / ".env"
            env_path.write_text("COGNEE_SERVICE_URL=https://legacy\n", encoding="utf-8")
            config_mod.write_env_vars(env_path, {"COGNEE_SERVICE_URL": ""})
            lines = env_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines, ["COGNEE_SERVICE_URL="])

    def test_no_values_writes_no_file(self):
        with _TmpHome() as home:
            env_path = home / ".env"
            config_mod.write_env_vars(env_path, {})
            self.assertFalse(env_path.exists())


class TestSaveConfig(unittest.TestCase):
    def test_merges_into_an_existing_file(self):
        with _TmpHome(file_config={"dataset": "old", "top_k": 3}) as home:
            config_mod.save_config({"dataset": "new"}, home)
            written = json.loads((home / "cognee.json").read_text(encoding="utf-8"))
        self.assertEqual(written["dataset"], "new")
        self.assertEqual(written["top_k"], 3)

    def test_skips_none_values(self):
        with _TmpHome(file_config={"dataset": "keep"}) as home:
            config_mod.save_config({"dataset": None, "top_k": 4}, home)
            written = json.loads((home / "cognee.json").read_text(encoding="utf-8"))
        self.assertEqual(written["dataset"], "keep")
        self.assertEqual(written["top_k"], 4)

    def test_provider_save_config_never_writes_secrets(self):
        provider = make_provider()
        with _TmpHome() as home:
            provider.save_config(
                {
                    "dataset": "hermes",
                    "api_key": "ck_secret",
                    "llm_api_key": "sk_secret",
                },
                str(home),
            )
            written = json.loads((home / "cognee.json").read_text(encoding="utf-8"))
        self.assertEqual(written, {"dataset": "hermes"})

    def test_provider_save_config_writes_nothing_when_only_secrets_are_given(self):
        provider = make_provider()
        with _TmpHome() as home:
            provider.save_config({"api_key": "ck", "llm_api_key": "sk"}, str(home))
            self.assertFalse((home / "cognee.json").exists())


class TestConfigPath(unittest.TestCase):
    def test_resolves_under_the_given_hermes_home(self):
        with _TmpHome() as home:
            self.assertEqual(config_mod.config_path(home), home / "cognee.json")

    def test_is_none_when_hermes_home_cannot_be_resolved(self):
        # hermes_constants is not importable outside a Hermes install.
        self.assertIsNone(config_mod.config_path(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
