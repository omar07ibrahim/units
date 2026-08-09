from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "tools" / "evidence" / "render.mjs"
PACKAGE = ROOT / "tools" / "evidence" / "package.json"
ASSETS = ROOT / "docs" / "assets"


def run_renderer(argument: str) -> subprocess.CompletedProcess[bytes]:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("the pinned Node.js renderer runtime is unavailable")
    return subprocess.run(
        [node, str(RENDERER), argument],
        cwd=ROOT,
        env={
            "HOME": str(ROOT / ".unitsentinel" / "renderer-test-home"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.defpath,
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=60,
    )


class ComparisonRendererTests(unittest.TestCase):
    def test_closed_comparison_mode_reproduces_seven_committed_outputs(
        self,
    ) -> None:
        completed = run_renderer("--check-comparison")
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        self.assertEqual(
            completed.stdout,
            (b"unitsentinel-evidence: verified 6 comparison PNG files and 1 GIF\n"),
        )
        self.assertEqual(completed.stderr, b"")

    def test_default_mode_counts_comparison_outputs(self) -> None:
        completed = run_renderer("--check")
        public_count = len(tuple(ASSETS.glob("*.svg")))
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        self.assertEqual(
            completed.stdout,
            (
                "unitsentinel-evidence: verified "
                f"{public_count} PNG files and 3 GIF files\n"
            ).encode(),
        )
        self.assertEqual(completed.stderr, b"")

    def test_renderer_rejects_an_unsupported_mode(self) -> None:
        completed = run_renderer("--comparison")
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, b"")
        self.assertEqual(
            completed.stderr,
            (
                b"unitsentinel-evidence: error: command-line arguments "
                b"are not supported\n"
            ),
        )

    def test_package_scripts_expose_fixed_comparison_modes(self) -> None:
        package = json.loads(PACKAGE.read_bytes())
        self.assertEqual(
            package["scripts"]["render:comparison"],
            "node render.mjs --render-comparison",
        )
        self.assertEqual(
            package["scripts"]["check:comparison"],
            "node render.mjs --check-comparison",
        )

    def test_renderer_uses_nonblocking_regular_file_preflight(self) -> None:
        source = RENDERER.read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("FS_CONSTANTS.O_NONBLOCK"), 2)
        self.assertIn(
            "FS_CONSTANTS.O_NOFOLLOW |\n          FS_CONSTANTS.O_NONBLOCK",
            source,
        )

    def test_renderer_has_an_aggregate_output_budget(self) -> None:
        source = RENDERER.read_text(encoding="utf-8")
        self.assertIn("const MAX_TOTAL_OUTPUT_BYTES = 268_435_456;", source)
        self.assertIn("function appendBoundedOutput(outputs, output)", source)
        self.assertGreaterEqual(source.count("appendBoundedOutput("), 5)


if __name__ == "__main__":
    unittest.main()
