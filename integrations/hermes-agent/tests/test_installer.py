"""Tests for the pip-install bridge (``cognee-hermes-install``).

Hermes discovers plugins by directory scan, so the installer must materialize
the exact plugin shape from the wheel: the root files at the plugin root and
the package beside them. The drift tests are the load-bearing ones — the wheel
ships *copies* of the repo-root plugin files, and a stale copy would publish a
plugin.yaml that disagrees with the repository.

Runs under pytest or standalone (``python3 tests/test_installer.py``); touches
nothing outside temporary directories.
"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cognee_integration_hermes import installer  # noqa: E402

_PACKAGE = ROOT / "cognee_integration_hermes"


class TestPackagedCopiesMatchTheRepo(unittest.TestCase):
    """The wheel's _plugin_root/* must be byte-identical to the repo root files."""

    def _assert_same(self, name):
        packaged = (_PACKAGE / "_plugin_root" / name).read_bytes()
        canonical = (ROOT / name).read_bytes()
        self.assertEqual(
            packaged,
            canonical,
            f"{name}: the packaged copy under _plugin_root/ has drifted from the "
            f"repo-root canonical file — re-copy it before publishing",
        )

    def test_plugin_yaml(self):
        self._assert_same("plugin.yaml")

    def test_cli_shim(self):
        self._assert_same("cli.py")

    def test_after_install(self):
        self._assert_same("after-install.md")

    def test_plugin_yaml_version_matches_pyproject(self):
        # The CHANGELOG contract: one version across plugin.yaml and pyproject.
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        manifest_version = next(
            line.split(":", 1)[1].strip()
            for line in manifest.splitlines()
            if line.startswith("version:")
        )
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f'version = "{manifest_version}"', pyproject)


class TestInstall(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)

    def test_lays_down_the_plugin_shape_hermes_scans_for(self):
        target = installer.install(self.home)
        self.assertEqual(target, self.home / "plugins" / "cognee")
        for name in ("plugin.yaml", "cli.py", "after-install.md"):
            self.assertTrue((target / name).is_file(), f"missing root file {name}")
        # The package itself, importable next to the root files.
        self.assertTrue((target / "cognee_integration_hermes" / "provider.py").is_file())
        self.assertTrue((target / "cognee_integration_hermes" / "exit_watcher.py").is_file())

    def test_bytecode_caches_are_not_shipped(self):
        target = installer.install(self.home)
        leftovers = list(target.rglob("__pycache__")) + list(target.rglob("*.pyc"))
        self.assertEqual(leftovers, [])

    def test_reinstall_replaces_the_package_wholesale(self):
        target = installer.install(self.home)
        stale = target / "cognee_integration_hermes" / "removed_in_next_version.py"
        stale.write_text("# stale module from an older release\n", encoding="utf-8")
        installer.install(self.home)
        self.assertFalse(
            stale.exists(),
            "reinstall must replace the package dir, not merge into it — "
            "stale modules from older releases would otherwise linger",
        )

    def test_missing_packaged_files_fail_loudly(self):
        with mock.patch.object(installer, "_ROOT_FILES", ("no-such-file.txt",)):
            with self.assertRaisesRegex(RuntimeError, "packaged plugin files missing"):
                installer.install(self.home)

    def test_main_honours_home_and_prints_next_steps(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = installer.main(["--home", str(self.home)])
        self.assertEqual(code, 0)
        self.assertTrue((self.home / "plugins" / "cognee" / "plugin.yaml").is_file())
        self.assertIn("hermes memory setup", out.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
