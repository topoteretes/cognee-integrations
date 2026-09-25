"""Exercise real metadata drift and malformed/missing release files."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "check_version_consistency.py"
_SPEC = importlib.util.spec_from_file_location("version_consistency", _SCRIPT)
checker = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(checker)


class VersionConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for manifest in checker.MANIFESTS.values():
            self.write_json(manifest, {"version": "1.2.3"})
        self.inventory = self.root / "integrations/inventory.yml"
        self.inventory.write_text(
            "integrations:\n"
            + "".join(
                f"  - slug: '{slug}'\n    current_version: \"1.2.3\" # release\n"
                for slug in checker.MANIFESTS
            ),
            encoding="utf-8",
        )
        self.plugin = {"source": "./integrations/claude-code", "version": "1.2.3"}
        self.write_json(checker.MARKETPLACE, {"plugins": [self.plugin]})

    def write_json(self, path, data):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data), encoding="utf-8")

    def test_matching_versions(self):
        self.assertEqual(checker.check_versions(self.root), [])

    def test_manifest_drift(self):
        for slug, manifest in checker.MANIFESTS.items():
            with self.subTest(slug=slug):
                self.write_json(manifest, {"version": "9.9.9"})
                self.assertTrue(any(slug in error for error in checker.check_versions(self.root)))
                self.write_json(manifest, {"version": "1.2.3"})

    def test_every_marketplace_entry_is_checked(self):
        # A later valid entry must not hide an earlier stale duplicate.
        stale = {**self.plugin, "version": "0.0.1"}
        self.write_json(checker.MARKETPLACE, {"plugins": [stale, self.plugin]})
        self.assertTrue(any("0.0.1" in error for error in checker.check_versions(self.root)))

    def test_missing_or_unversioned_marketplace_fails(self):
        for data in ({"plugins": []}, {"plugins": [{"source": self.plugin["source"]}]}):
            self.write_json(checker.MARKETPLACE, data)
            self.assertTrue(checker.check_versions(self.root))
        (self.root / checker.MARKETPLACE).unlink()
        self.assertTrue(checker.check_versions(self.root))

    def test_missing_or_malformed_manifest_fails(self):
        manifest = self.root / checker.MANIFESTS["codex"]
        manifest.unlink()
        self.assertTrue(checker.check_versions(self.root))
        manifest.write_text("broken json", encoding="utf-8")
        self.assertTrue(checker.check_versions(self.root))

    def test_conflicting_inventory_duplicate_fails(self):
        with self.inventory.open("a", encoding="utf-8") as stream:
            stream.write("  - slug: codex\n    current_version: 9.9.9\n")
        self.assertTrue(any("conflicting" in error for error in checker.check_versions(self.root)))

    def test_missing_inventory_version_fails(self):
        self.inventory.write_text("integrations:\n", encoding="utf-8")
        self.assertEqual(len(checker.check_versions(self.root)), len(checker.MANIFESTS))


if __name__ == "__main__":
    unittest.main()
