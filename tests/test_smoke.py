"""Lightweight offline smoke tests for Agent Society."""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "agent_society.py"


class AgentSocietySmokeTests(unittest.TestCase):
    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_help(self) -> None:
        result = self.run_script("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--brain", result.stdout)
        self.assertIn("--ticks", result.stdout)

    def test_short_offline_run_writes_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent-society-test-") as temp_dir:
            state_dir = Path(temp_dir) / "state"
            result = self.run_script(
                "--brain", "scripted",
                "--ticks", "2",
                "--no-viewer",
                "--no-browser",
                "--state", str(state_dir),
            )
            self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
            self.assertTrue((state_dir / "state.json").exists())
            self.assertTrue((state_dir / "chronicle.md").exists())
            self.assertTrue((state_dir / "events.jsonl").exists())
            self.assertTrue((state_dir / "thoughts.jsonl").exists())
            self.assertTrue((state_dir / "snapshots.jsonl").exists())
            self.assertTrue((state_dir / "agents" / "A" / "history.jsonl").exists())
            self.assertIn("Done.", result.stdout)

    def test_viewer_javascript_syntax(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is not installed; browser syntax check skipped")
        spec = importlib.util.spec_from_file_location("agent_society_viewer_test", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        scripts = re.findall(r"<script(?:\\s[^>]*)?>(.*?)</script>", module.VIEWER_HTML, re.S)
        inline_scripts = [script for script in scripts if script.strip()]
        self.assertTrue(inline_scripts, "No inline viewer script found")
        result = subprocess.run([node, "--check"], input=inline_scripts[-1], text=True,
                                capture_output=True, check=False, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + "\\n" + result.stderr)

    def test_bad_marker_is_viewer_only_and_observations_are_blank_slate(self) -> None:
        spec = importlib.util.spec_from_file_location("agent_society_under_test", SCRIPT)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        import sys
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        world = module.World(seed=13)
        world.add_agent("A", None, "", "house")
        world.add_agent("B", None, "", "house")
        world.bad_agent_id = "B"
        observation = world.observe(world.agents["A"])
        self.assertNotIn("bad_agent_id", observation)
        self.assertNotIn("hidden trait", observation.lower())
        self.assertNotIn("farmer", observation.lower())
        self.assertNotIn("baker", observation.lower())
        self.assertNotIn("priest", observation.lower())
        snapshot = world.snapshot()
        self.assertFalse(snapshot["agents"]["A"]["is_bad"])
        self.assertTrue(snapshot["agents"]["B"]["is_bad"])


if __name__ == "__main__":
    unittest.main()
