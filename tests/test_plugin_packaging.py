from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PluginPackagingTests(unittest.TestCase):
    def test_plugin_manifest_is_filled_and_points_to_skill(self) -> None:
        manifest_path = ROOT / "plugins" / "harness-cache" / ".codex-plugin" / "plugin.json"
        manifest = json.loads(manifest_path.read_text())

        self.assertEqual(manifest["name"], "harness-cache")
        self.assertEqual(manifest["version"], "0.1.0")
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["interface"]["displayName"], "Harness Cache")
        self.assertNotIn("[TODO", json.dumps(manifest))
        self.assertTrue((ROOT / "plugins" / "harness-cache" / "skills" / "harness-cache" / "SKILL.md").exists())

    def test_marketplace_entry_points_to_repo_local_plugin(self) -> None:
        marketplace = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text())
        entry = marketplace["plugins"][0]

        self.assertEqual(marketplace["name"], "harness-cache-local")
        self.assertEqual(entry["name"], "harness-cache")
        self.assertEqual(entry["source"]["path"], "./plugins/harness-cache")
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")

    def test_plugin_demo_script_runs_and_inspects_cache(self) -> None:
        script = ROOT / "plugins" / "harness-cache" / "scripts" / "run_demo.py"
        demo = subprocess.run(
            [sys.executable, str(script)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Saves tokens: True", demo.stdout)

        inspected = subprocess.run(
            [sys.executable, str(script), "--inspect"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(inspected.stdout)
        self.assertTrue(payload["comparison"]["saves_tokens"])
        self.assertIn("ptr_orders_reserve", payload["stores"]["hot_set"])


if __name__ == "__main__":
    unittest.main()
