from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from test_onnx_adapter import serialize, speed_model
from unitsentinel import cli
from unitsentinel.canonical import canonical_json_bytes
from unitsentinel.graph_codec import decode_graph
from unitsentinel.onnx_adapter import (
    MAX_ONNX_MODEL_BYTES,
    OnnxDependencyError,
)


class ImportOnnxCLITests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.model_path = self.directory / "speed.onnx"
        self.graph_path = self.directory / "speed.graph.json"
        self.model_path.write_bytes(serialize(speed_model()))

    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()

    def test_text_import_writes_and_binds_a_real_canonical_graph(self) -> None:
        exit_code, stdout, stderr = self.invoke(
            "import-onnx",
            str(self.model_path),
            "--graph",
            str(self.graph_path),
        )

        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        self.assertEqual(stderr, "")
        self.assertIn("UnitSentinel ONNX import: IMPORTED\n", stdout)
        self.assertIn("checker: onnx.checker.check_model 1.22.0", stdout)
        self.assertIn("model executed: no\n", stdout)
        self.assertIn("external tensor data: rejected\n", stdout)
        self.assertIn("derive-speed | Div -> divide", stdout)
        self.assertIn("graph output: written\n", stdout)
        graph = decode_graph(self.graph_path.read_bytes())
        self.assertEqual(graph.graph_id, "onnx-speed-contract")
        self.assertEqual(graph.outputs, ("speed",))

    def test_json_import_is_canonical_and_self_describing(self) -> None:
        exit_code, stdout, stderr = self.invoke(
            "import-onnx",
            str(self.model_path),
            "--graph",
            str(self.graph_path),
            "--json",
        )

        record = json.loads(stdout)
        graph = decode_graph(self.graph_path.read_bytes())
        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        self.assertEqual(stderr, "")
        self.assertEqual(record["schema"], cli.IMPORT_ONNX_OUTPUT_SCHEMA)
        self.assertEqual(record["exit_code"], cli.EXIT_SUCCESS)
        self.assertEqual(record["graph_output"], "written")
        self.assertEqual(record["import"]["record"]["graph"]["sha256"], graph.digest)
        self.assertEqual(record["import"]["record"]["model"]["model_executed"], False)
        self.assertEqual(
            stdout,
            canonical_json_bytes(record).decode("utf-8") + "\n",
        )

    def test_existing_graph_is_never_overwritten_or_partially_reported(self) -> None:
        self.graph_path.write_bytes(b"sentinel")
        exit_code, stdout, stderr = self.invoke(
            "import-onnx",
            str(self.model_path),
            "--graph",
            str(self.graph_path),
            "--json",
        )

        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertEqual(self.graph_path.read_bytes(), b"sentinel")
        self.assertEqual(
            stderr,
            "unitsentinel: error: graph output already exists\n",
        )

    def test_invalid_model_and_missing_extra_have_stable_input_failures(self) -> None:
        invalid = self.directory / "invalid.onnx"
        invalid.write_bytes(b"not an ONNX protobuf")
        exit_code, stdout, stderr = self.invoke(
            "import-onnx",
            str(invalid),
            "--graph",
            str(self.graph_path),
        )
        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertIn("ONNX model input is invalid", stderr)
        self.assertNotIn(str(invalid), stderr)

        with patch.object(
            cli,
            "import_onnx_model",
            side_effect=OnnxDependencyError(
                "ONNX support is unavailable; install unitsentinel[onnx]"
            ),
        ):
            exit_code, stdout, stderr = self.invoke(
                "import-onnx",
                str(self.model_path),
                "--graph",
                str(self.graph_path),
            )
        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            (
                "unitsentinel: error: ONNX support is unavailable; "
                "install unitsentinel[onnx]\n"
            ),
        )

    def test_model_input_uses_the_bounded_regular_file_boundary(self) -> None:
        oversized = self.directory / "oversized.onnx"
        with oversized.open("wb") as stream:
            stream.truncate(MAX_ONNX_MODEL_BYTES + 1)
        symlink = self.directory / "symlink.onnx"
        symlink.symlink_to(self.model_path)
        fifo = self.directory / "fifo.onnx"
        os.mkfifo(fifo)

        for path, message in (
            (oversized, "exceeds the byte limit"),
            (symlink, "could not be opened"),
            (fifo, "must be a regular file"),
        ):
            with self.subTest(path=path.name):
                exit_code, stdout, stderr = self.invoke(
                    "import-onnx",
                    str(path),
                    "--graph",
                    str(self.graph_path),
                )
                self.assertEqual(exit_code, cli.EXIT_INPUT)
                self.assertEqual(stdout, "")
                self.assertIn(message, stderr)
                self.assertNotIn(str(path), stderr)

    def test_unexpected_adapter_result_is_an_internal_failure(self) -> None:
        with patch.object(
            cli,
            "import_onnx_model",
            return_value=object(),
        ):
            exit_code, stdout, stderr = self.invoke(
                "import-onnx",
                str(self.model_path),
                "--graph",
                str(self.graph_path),
            )
        self.assertEqual(exit_code, cli.EXIT_INTERNAL)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "unitsentinel: error: ONNX import returned an unexpected result\n",
        )

    def test_help_exposes_the_import_contract(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            cli.main(("import-onnx", "--help"))
        self.assertEqual(raised.exception.code, 0)
        self.assertIn("--graph FILE", stdout.getvalue())
        self.assertIn("--json", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
