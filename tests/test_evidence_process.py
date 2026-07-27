from __future__ import annotations

import sys
import unittest

from tools.evidence import generate
from tools.evidence.generate import EvidenceError


class BoundedEvidenceProcessTests(unittest.TestCase):
    def test_capture_returns_exact_stdout_stderr_and_exit(self) -> None:
        return_code, stdout, stderr = generate._capture_bounded_process(
            (
                sys.executable,
                "-c",
                (
                    "import sys;"
                    "sys.stdout.buffer.write(b'out');"
                    "sys.stderr.buffer.write(b'err')"
                ),
            ),
            cwd=generate.ROOT,
            environment={"PYTHONHASHSEED": "0"},
            timeout_seconds=5,
            stdout_limit=3,
            stderr_limit=3,
        )

        self.assertEqual(return_code, 0)
        self.assertEqual(stdout, b"out")
        self.assertEqual(stderr, b"err")

    def test_capture_terminates_stdout_and_stderr_overflow(self) -> None:
        cases = (
            (1, "stdout"),
            (2, "stderr"),
        )
        for descriptor, label in cases:
            with (
                self.subTest(stream=label),
                self.assertRaisesRegex(
                    EvidenceError,
                    f"process {label} exceeded its capture limit",
                ),
            ):
                generate._capture_bounded_process(
                    (
                        sys.executable,
                        "-c",
                        f"import os;os.write({descriptor}, b'x' * 33)",
                    ),
                    cwd=generate.ROOT,
                    environment={"PYTHONHASHSEED": "0"},
                    timeout_seconds=5,
                    stdout_limit=32,
                    stderr_limit=32,
                )

    def test_capture_terminates_a_timed_out_child(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "time limit"):
            generate._capture_bounded_process(
                (
                    sys.executable,
                    "-c",
                    "import time;time.sleep(2)",
                ),
                cwd=generate.ROOT,
                environment={"PYTHONHASHSEED": "0"},
                timeout_seconds=0.05,
                stdout_limit=32,
                stderr_limit=32,
            )

    def test_capture_rejects_invalid_limits_before_spawning(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "limits are invalid"):
            generate._capture_bounded_process(
                (sys.executable, "-c", "raise SystemExit(0)"),
                cwd=generate.ROOT,
                environment={"PYTHONHASHSEED": "0"},
                timeout_seconds=0,
                stdout_limit=0,
                stderr_limit=0,
            )


if __name__ == "__main__":
    unittest.main()
