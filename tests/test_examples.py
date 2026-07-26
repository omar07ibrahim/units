from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from unitsentinel import decode_graph

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ExampleTests(unittest.TestCase):
    def test_speed_contract_generator_emits_exact_decodable_bytes(self) -> None:
        environment = {
            "PATH": os.defpath,
            "PYTHONPATH": str(REPOSITORY_ROOT / "src"),
        }
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                str(REPOSITORY_ROOT / "examples" / "build_speed_contract.py"),
            ],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=5,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        self.assertEqual(completed.stderr, b"")
        self.assertFalse(completed.stdout.endswith(b"\n"))
        graph = decode_graph(completed.stdout)
        self.assertEqual(graph.graph_id, "speed-contract")
        self.assertEqual(
            graph.digest,
            "aecbeff2ce89cfd7b2aab6a0414ec307a5061577f4b8b0d1c53298f896569546",
        )


if __name__ == "__main__":
    unittest.main()
