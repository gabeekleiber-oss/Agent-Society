"""Lightweight offline smoke tests for Agent Society."""
from __future__ import annotations

import os
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
            self.assertIn("Done.", result.stdout)


if __name__ == "__main__":
    unittest.main()
