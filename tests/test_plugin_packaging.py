from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "plugins" / "harness-cache" / "scripts" / "run_demo.py"


def load_plugin_script():
    spec = importlib.util.spec_from_file_location("harness_cache_plugin_run_demo", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load plugin script")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
        demo = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Saves tokens: True", demo.stdout)

        inspected = subprocess.run(
            [sys.executable, str(SCRIPT), "--inspect"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(inspected.stdout)
        self.assertTrue(payload["comparison"]["saves_tokens"])
        self.assertIn("ptr_orders_reserve", payload["stores"]["hot_set"])

    def test_plugin_measurement_dry_run_builds_cache_and_baseline_prompts(self) -> None:
        dry_run = subprocess.run(
            [sys.executable, str(SCRIPT), "--measure-codex", "--dry-run"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(dry_run.stdout)

        self.assertIn("Use harness-cache", payload["prompts"]["with_harness_cache"])
        self.assertIn("no-cache baseline", payload["prompts"]["without_harness_cache"])
        self.assertGreater(
            len(payload["prompts"]["without_harness_cache"]),
            len(payload["prompts"]["with_harness_cache"]),
        )

    def test_codex_token_usage_log_parser(self) -> None:
        script = load_plugin_script()
        line = (
            "turn.id=turn_123 model=gpt "
            "codex.turn.token_usage.input_tokens=100 "
            "codex.turn.token_usage.cached_input_tokens=20 "
            "codex.turn.token_usage.non_cached_input_tokens=80 "
            "codex.turn.token_usage.output_tokens=7 "
            "codex.turn.token_usage.reasoning_output_tokens=3 "
            "codex.turn.token_usage.total_tokens=107"
        )

        usage = script.parse_token_usage_line(line)

        self.assertEqual(usage["turn_id"], "turn_123")
        self.assertEqual(usage["input_tokens"], 100)
        self.assertEqual(usage["cached_input_tokens"], 20)
        self.assertEqual(usage["non_cached_input_tokens"], 80)
        self.assertEqual(usage["output_tokens"], 7)
        self.assertEqual(usage["reasoning_output_tokens"], 3)
        self.assertEqual(usage["total_tokens"], 107)

    def test_codex_json_usage_parser(self) -> None:
        script = load_plugin_script()
        stdout = "\n".join(
            [
                '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}',
                (
                    '{"type":"turn.completed","usage":'
                    '{"input_tokens":50,"cached_input_tokens":8,'
                    '"output_tokens":5,"reasoning_output_tokens":2}}'
                ),
            ]
        )

        usage = script.parse_codex_json_usage(stdout)

        self.assertEqual(usage["input_tokens"], 50)
        self.assertEqual(usage["cached_input_tokens"], 8)
        self.assertEqual(usage["non_cached_input_tokens"], 42)
        self.assertEqual(usage["output_tokens"], 5)
        self.assertEqual(usage["reasoning_output_tokens"], 2)
        self.assertEqual(usage["total_tokens"], 55)


if __name__ == "__main__":
    unittest.main()
