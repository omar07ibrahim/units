from __future__ import annotations

import io
import json
import os
import runpy
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from examples.build_speed_contract import build_graph
from unitsentinel import cli
from unitsentinel.canonical import canonical_json_bytes, sha256_hex
from unitsentinel.certificate import (
    MAX_CERTIFICATE_BYTES,
    CertificateError,
    ProofCertificate,
    create_certificate,
    encode_certificate,
)
from unitsentinel.domain import UnitSentinelError
from unitsentinel.graph import ComputationGraph, Node, Operation, ScalarType, ValueSpec
from unitsentinel.graph_codec import MAX_GRAPH_BYTES, encode_graph
from unitsentinel.registry import BUILTIN_REGISTRY
from unitsentinel.replay import (
    CertificateReplay,
    CertificateReplayError,
    ReplayReason,
    ReplayStatus,
    replay_certificate,
)
from unitsentinel.verification import (
    SolverLimits,
    UnknownReason,
    VerificationResult,
    VerificationStatus,
)
from unitsentinel.version import VERSION


def ambiguous_graph() -> ComputationGraph:
    return ComputationGraph(
        graph_id="ambiguous-identity",
        values=(
            ValueSpec("input", ScalarType.FLOAT64, ()),
            ValueSpec("output", ScalarType.FLOAT64, ()),
        ),
        inputs=("input",),
        nodes=(
            Node(
                "copy-input",
                Operation.IDENTITY,
                ("input",),
                "output",
            ),
        ),
        outputs=("output",),
    )


def conflicting_graph() -> ComputationGraph:
    return ComputationGraph(
        graph_id="invalid-addition",
        values=(
            ValueSpec("distance", ScalarType.FLOAT64, (), "meter"),
            ValueSpec("duration", ScalarType.FLOAT64, (), "second"),
            ValueSpec("sum", ScalarType.FLOAT64, (), "meter"),
        ),
        inputs=("distance", "duration"),
        nodes=(
            Node(
                "add-incompatible-values",
                Operation.ADD,
                ("distance", "duration"),
                "sum",
            ),
        ),
        outputs=("sum",),
    )


class CLITestCase(unittest.TestCase):
    graph = build_graph()
    graph_bytes = encode_graph(graph)
    certificate = create_certificate(graph)
    certificate_bytes = encode_certificate(certificate)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.graph_path = self.directory / "graph.json"
        self.certificate_path = self.directory / "certificate.json"
        self.graph_path.write_bytes(self.graph_bytes)
        self.certificate_path.write_bytes(self.certificate_bytes)

    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = cli.main(arguments)
        return exit_code, stdout.getvalue(), stderr.getvalue()


class ArgumentContractTests(CLITestCase):
    def test_missing_and_unknown_commands_have_stable_usage_failures(self) -> None:
        for arguments in ((), ("unknown-secret-command",)):
            with self.subTest(arguments=arguments):
                exit_code, stdout, stderr = self.invoke(*arguments)
                self.assertEqual(exit_code, cli.EXIT_USAGE)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    stderr,
                    (
                        "unitsentinel: error: invalid command-line arguments; "
                        "use --help\n"
                    ),
                )
                self.assertNotIn("secret", stderr)

    def test_invalid_expected_digest_fails_before_opening_inputs(self) -> None:
        exit_code, stdout, stderr = self.invoke(
            "replay",
            "missing-certificate",
            "--graph",
            "missing-graph",
            "--expect-sha256",
            "ABC",
        )

        self.assertEqual(exit_code, cli.EXIT_USAGE)
        self.assertEqual(stdout, "")
        self.assertIn("invalid command-line arguments", stderr)

    def test_help_and_version_use_successful_argparse_exits(self) -> None:
        for arguments, expected in (
            (("--help",), "Verify exact dimensional contracts"),
            (("--version",), f"unitsentinel {VERSION}"),
            (("verify", "--help"), "--certificate"),
            (("replay", "--help"), "--strict-toolchain"),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                self.subTest(arguments=arguments),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(SystemExit) as raised,
            ):
                cli.main(arguments)
            self.assertEqual(raised.exception.code, 0)
            self.assertIn(expected, stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_package_module_entrypoint_matches_the_cli_version(self) -> None:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(sys, "argv", ["unitsentinel", "--version"]),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            runpy.run_module("unitsentinel", run_name="__main__")

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stdout.getvalue(), f"unitsentinel {VERSION}\n")
        self.assertEqual(stderr.getvalue(), "")

    def test_boundary_redacts_interrupt_domain_and_unexpected_failures(self) -> None:
        cases = (
            (
                KeyboardInterrupt(),
                cli.EXIT_INTERRUPTED,
                "unitsentinel: interrupted\n",
            ),
            (
                UnitSentinelError("private /home/omar path"),
                cli.EXIT_INTERNAL,
                "unitsentinel: error: internal contract failure\n",
            ),
            (
                RuntimeError("private /home/omar path"),
                cli.EXIT_INTERNAL,
                "unitsentinel: error: internal failure\n",
            ),
        )
        for failure, expected_exit, expected_error in cases:
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(cli, "_dispatch", side_effect=failure),
            ):
                exit_code, stdout, stderr = self.invoke("verify", str(self.graph_path))
            self.assertEqual(exit_code, expected_exit)
            self.assertEqual(stdout, "")
            self.assertEqual(stderr, expected_error)
            self.assertNotIn("omar", stderr.lower())


class VerifyCommandTests(CLITestCase):
    def test_positive_text_output_exposes_exact_contracts_and_digests(self) -> None:
        exit_code, stdout, stderr = self.invoke("verify", str(self.graph_path))

        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        self.assertEqual(stderr, "")
        self.assertIn("UnitSentinel verification: VERIFIED\n", stdout)
        self.assertIn(f"graph sha256: {self.graph.digest}\n", stdout)
        self.assertIn(f"result sha256: {self.certificate.result.digest}\n", stdout)
        self.assertIn("solver: z3 4.16.0 (2 checks)\n", stdout)
        self.assertIn(
            "raw-speed | length^1 time^-1 | linear | scale=5/18 offset=0",
            stdout,
        )
        self.assertIn(
            "si-speed | length^1 time^-1 | linear | scale=1 offset=0",
            stdout,
        )
        self.assertIn(f"certificate sha256: {self.certificate.digest}\n", stdout)
        self.assertIn("certificate authentication: not-provided\n", stdout)
        self.assertIn("certificate output: not-requested\n", stdout)

    def test_positive_json_output_is_canonical_and_self_describing(self) -> None:
        exit_code, stdout, stderr = self.invoke(
            "verify",
            str(self.graph_path),
            "--json",
        )

        record = json.loads(stdout)
        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        self.assertEqual(stderr, "")
        self.assertEqual(record["schema"], cli.VERIFY_OUTPUT_SCHEMA)
        self.assertEqual(record["result"]["record"]["status"], "verified")
        self.assertEqual(
            record["result"]["sha256"],
            self.certificate.result.digest,
        )
        self.assertEqual(
            record["certificate"],
            {
                "authentication": "not-provided",
                "schema": "unitsentinel.proof-certificate/v1",
                "sha256": self.certificate.digest,
            },
        )
        self.assertEqual(record["certificate_output"], "not-requested")
        self.assertEqual(record["exit_code"], cli.EXIT_SUCCESS)
        self.assertEqual(record["graph"]["graph_id"], self.graph.graph_id)
        self.assertEqual(record["tool"]["version"], VERSION)
        self.assertEqual(
            stdout,
            canonical_json_bytes(record).decode("utf-8") + "\n",
        )

    def test_certificate_output_is_exact_private_atomic_and_temp_free(self) -> None:
        output = self.directory / "issued.cert.json"
        previous_umask = os.umask(0o777)
        try:
            exit_code, stdout, stderr = self.invoke(
                "verify",
                str(self.graph_path),
                "--certificate",
                str(output),
            )
        finally:
            os.umask(previous_umask)

        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        self.assertEqual(stderr, "")
        self.assertIn("certificate output: written\n", stdout)
        self.assertEqual(output.read_bytes(), self.certificate_bytes)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        self.assertEqual(list(self.directory.glob(".unitsentinel-*.tmp")), [])

    def test_partial_kernel_writes_are_completed_before_publication(self) -> None:
        output = self.directory / "short-writes.cert.json"
        real_write = os.write

        def short_write(descriptor: int, payload: object) -> int:
            view = memoryview(payload)  # type: ignore[arg-type]
            return real_write(descriptor, view[:17])

        with patch.object(cli.os, "write", side_effect=short_write):
            exit_code, stdout, stderr = self.invoke(
                "verify",
                str(self.graph_path),
                "--certificate",
                str(output),
            )

        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        self.assertEqual(stderr, "")
        self.assertIn("certificate output: written", stdout)
        self.assertEqual(output.read_bytes(), self.certificate_bytes)

    def test_existing_output_is_never_overwritten_or_leaked_to_stdout(self) -> None:
        output = self.directory / "existing.cert.json"
        output.write_bytes(b"owner-data")

        exit_code, stdout, stderr = self.invoke(
            "verify",
            str(self.graph_path),
            "--certificate",
            str(output),
            "--json",
        )

        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "unitsentinel: error: certificate output already exists\n",
        )
        self.assertEqual(output.read_bytes(), b"owner-data")
        self.assertEqual(list(self.directory.glob(".unitsentinel-*.tmp")), [])

    def test_negative_results_have_distinct_exits_and_never_write_certificates(
        self,
    ) -> None:
        cases = (
            (ambiguous_graph(), cli.EXIT_UNDERCONSTRAINED, "UNDERCONSTRAINED"),
            (conflicting_graph(), cli.EXIT_CONFLICT, "CONFLICT"),
        )
        for index, (graph, expected_exit, expected_status) in enumerate(cases):
            graph_path = self.directory / f"negative-{index}.json"
            output = self.directory / f"negative-{index}.cert.json"
            graph_path.write_bytes(encode_graph(graph))
            with self.subTest(status=expected_status):
                exit_code, stdout, stderr = self.invoke(
                    "verify",
                    str(graph_path),
                    "--certificate",
                    str(output),
                )
            self.assertEqual(exit_code, expected_exit)
            self.assertEqual(stderr, "")
            self.assertIn(
                f"UnitSentinel verification: {expected_status}",
                stdout,
            )
            self.assertNotIn("certificate sha256:", stdout)
            self.assertFalse(output.exists())

    def test_unknown_result_is_an_indeterminate_noncertificate_outcome(self) -> None:
        unknown = VerificationResult(
            status=VerificationStatus.UNKNOWN,
            graph_digest=self.graph.digest,
            registry_digest=BUILTIN_REGISTRY.digest,
            solver_version=self.certificate.result.solver_version,
            limits=SolverLimits(),
            checks_performed=0,
            unknown_reason=UnknownReason.RESOURCE_LIMIT,
        )
        with patch.object(
            cli,
            "_create_certificate_attempt",
            return_value=(unknown, None),
        ):
            exit_code, stdout, stderr = self.invoke(
                "verify",
                str(self.graph_path),
            )

        self.assertEqual(exit_code, cli.EXIT_INDETERMINATE)
        self.assertEqual(stderr, "")
        self.assertIn("UnitSentinel verification: UNKNOWN", stdout)
        self.assertIn("unknown reason: resource-limit", stdout)
        self.assertNotIn("certificate sha256:", stdout)

    def test_internal_issuance_failure_is_redacted(self) -> None:
        with patch.object(
            cli,
            "_create_certificate_attempt",
            side_effect=CertificateError("solver leaked /private/path"),
        ):
            exit_code, stdout, stderr = self.invoke(
                "verify",
                str(self.graph_path),
            )

        self.assertEqual(exit_code, cli.EXIT_INTERNAL)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "unitsentinel: error: verification could not be completed safely\n",
        )
        self.assertNotIn("private", stderr)


class ReplayCommandTests(CLITestCase):
    def test_reproduced_text_output_shows_current_semantic_recheck(self) -> None:
        exit_code, stdout, stderr = self.invoke(
            "replay",
            str(self.certificate_path),
            "--graph",
            str(self.graph_path),
        )

        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        self.assertEqual(stderr, "")
        self.assertIn("UnitSentinel replay: REPRODUCED\n", stdout)
        self.assertIn("reason: none\n", stdout)
        self.assertIn(f"certificate sha256: {self.certificate.digest}\n", stdout)
        self.assertIn("certificate authentication: not-provided\n", stdout)
        self.assertIn("toolchain match: yes (strict=no)\n", stdout)
        self.assertIn("fresh result: verified ", stdout)

    def test_reproduced_json_output_is_canonical_and_self_describing(self) -> None:
        exit_code, stdout, stderr = self.invoke(
            "replay",
            str(self.certificate_path),
            "--graph",
            str(self.graph_path),
            "--strict-toolchain",
            "--json",
        )

        record = json.loads(stdout)
        self.assertEqual(exit_code, cli.EXIT_SUCCESS)
        self.assertEqual(stderr, "")
        self.assertEqual(record["schema"], cli.REPLAY_OUTPUT_SCHEMA)
        self.assertEqual(record["report"]["record"]["status"], "reproduced")
        self.assertTrue(record["report"]["record"]["strict_toolchain"])
        self.assertEqual(
            record["report"]["sha256"],
            sha256_hex(canonical_json_bytes(record["report"]["record"])),
        )
        self.assertEqual(record["certificate"]["authentication"], "not-provided")
        self.assertEqual(record["exit_code"], cli.EXIT_SUCCESS)
        self.assertEqual(
            stdout,
            canonical_json_bytes(record).decode("utf-8") + "\n",
        )

    def test_graph_binding_mismatch_is_a_report_not_a_cli_error(self) -> None:
        other_graph = ComputationGraph(
            graph_id="different-speed-contract",
            values=self.graph.values,
            inputs=self.graph.inputs,
            nodes=self.graph.nodes,
            outputs=self.graph.outputs,
        )
        other_path = self.directory / "other-graph.json"
        other_path.write_bytes(encode_graph(other_graph))

        exit_code, stdout, stderr = self.invoke(
            "replay",
            str(self.certificate_path),
            "--graph",
            str(other_path),
            "--json",
        )

        record = json.loads(stdout)
        self.assertEqual(exit_code, cli.EXIT_MISMATCH)
        self.assertEqual(stderr, "")
        self.assertEqual(record["report"]["record"]["status"], "mismatch")
        self.assertEqual(
            record["report"]["record"]["reason"],
            "graph-digest-mismatch",
        )
        self.assertIsNone(record["report"]["record"]["fresh_result"])
        self.assertEqual(record["exit_code"], cli.EXIT_MISMATCH)

    def test_expected_digest_matches_or_short_circuits_before_graph_read(self) -> None:
        exit_code, stdout, stderr = self.invoke(
            "replay",
            str(self.certificate_path),
            "--graph",
            str(self.graph_path),
            "--expect-sha256",
            self.certificate.digest,
        )
        self.assertEqual((exit_code, stderr), (cli.EXIT_SUCCESS, ""))
        self.assertIn("REPRODUCED", stdout)

        wrong_digest = "0" * 64
        if wrong_digest == self.certificate.digest:
            wrong_digest = "1" * 64
        with patch.object(
            cli,
            "_decode_graph_file",
            side_effect=AssertionError("graph must not be read"),
        ) as graph_decoder:
            exit_code, stdout, stderr = self.invoke(
                "replay",
                str(self.certificate_path),
                "--graph",
                "private-graph-name",
                "--expect-sha256",
                wrong_digest,
            )

        self.assertEqual(exit_code, cli.EXIT_MISMATCH)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            (
                "unitsentinel: error: certificate sha256 does not match "
                "the expected digest\n"
            ),
        )
        graph_decoder.assert_not_called()

    def test_strict_toolchain_rejects_a_structurally_valid_old_claim(self) -> None:
        old_claim = ProofCertificate(
            registry_version=self.certificate.registry_version,
            verifier_version="0.0.1",
            constraints=self.certificate.constraints,
            result=self.certificate.result,
        )
        old_path = self.directory / "old-toolchain.cert.json"
        old_path.write_bytes(encode_certificate(old_claim))

        exit_code, stdout, stderr = self.invoke(
            "replay",
            str(old_path),
            "--graph",
            str(self.graph_path),
            "--strict-toolchain",
        )

        self.assertEqual(exit_code, cli.EXIT_MISMATCH)
        self.assertEqual(stderr, "")
        self.assertIn("UnitSentinel replay: MISMATCH", stdout)
        self.assertIn("reason: toolchain-mismatch", stdout)
        self.assertIn("toolchain match: no (strict=yes)", stdout)
        self.assertNotIn("fresh result:", stdout)

    def test_indeterminate_replay_has_exit_three_and_fresh_unknown_evidence(
        self,
    ) -> None:
        baseline = replay_certificate(self.certificate, self.graph)
        fresh_unknown = VerificationResult(
            status=VerificationStatus.UNKNOWN,
            graph_digest=self.graph.digest,
            registry_digest=BUILTIN_REGISTRY.digest,
            solver_version=baseline.current_solver_version,
            limits=SolverLimits(),
            checks_performed=0,
            unknown_reason=UnknownReason.RESOURCE_LIMIT,
        )
        indeterminate = CertificateReplay(
            status=ReplayStatus.INDETERMINATE,
            reason=ReplayReason.FRESH_UNKNOWN,
            certificate_digest=self.certificate.digest,
            graph_digest=self.graph.digest,
            registry_digest=BUILTIN_REGISTRY.digest,
            registry_version=BUILTIN_REGISTRY.version,
            strict_toolchain=False,
            certificate_verifier_version=self.certificate.verifier_version,
            certificate_solver_version=self.certificate.result.solver_version,
            current_verifier_version=VERSION,
            current_solver_version=baseline.current_solver_version,
            toolchain_match=True,
            fresh_result=fresh_unknown,
        )
        with patch.object(
            cli,
            "replay_certificate",
            return_value=indeterminate,
        ):
            exit_code, stdout, stderr = self.invoke(
                "replay",
                str(self.certificate_path),
                "--graph",
                str(self.graph_path),
            )

        self.assertEqual(exit_code, cli.EXIT_INDETERMINATE)
        self.assertEqual(stderr, "")
        self.assertIn("UnitSentinel replay: INDETERMINATE", stdout)
        self.assertIn("reason: fresh-unknown", stdout)
        self.assertIn("fresh result: unknown ", stdout)

    def test_internal_replay_failure_is_redacted(self) -> None:
        with patch.object(
            cli,
            "replay_certificate",
            side_effect=CertificateReplayError("private solver detail"),
        ):
            exit_code, stdout, stderr = self.invoke(
                "replay",
                str(self.certificate_path),
                "--graph",
                str(self.graph_path),
            )

        self.assertEqual(exit_code, cli.EXIT_INTERNAL)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            ("unitsentinel: error: certificate replay could not be completed safely\n"),
        )
        self.assertNotIn("private", stderr)


class InputBoundaryTests(CLITestCase):
    def test_missing_paths_are_redacted(self) -> None:
        private_name = self.directory / "omar-secret-graph.json"
        exit_code, stdout, stderr = self.invoke("verify", str(private_name))

        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "unitsentinel: error: graph input could not be opened\n",
        )
        self.assertNotIn("omar", stderr)

    def test_malformed_graph_and_certificate_are_distinguished(self) -> None:
        malformed_graph = self.directory / "malformed-graph.json"
        malformed_certificate = self.directory / "malformed-certificate.json"
        malformed_graph.write_bytes(b"{}")
        malformed_certificate.write_bytes(b"{}")

        graph_exit, graph_stdout, graph_stderr = self.invoke(
            "verify",
            str(malformed_graph),
        )
        certificate_exit, certificate_stdout, certificate_stderr = self.invoke(
            "replay",
            str(malformed_certificate),
            "--graph",
            str(self.graph_path),
        )

        self.assertEqual(graph_exit, cli.EXIT_INPUT)
        self.assertEqual(certificate_exit, cli.EXIT_INPUT)
        self.assertEqual(graph_stdout, "")
        self.assertEqual(certificate_stdout, "")
        self.assertIn("graph input is invalid:", graph_stderr)
        self.assertIn("certificate input is invalid:", certificate_stderr)

    def test_oversized_graph_and_certificate_stop_before_decoding(self) -> None:
        oversized_graph = self.directory / "oversized-graph.json"
        oversized_certificate = self.directory / "oversized-certificate.json"
        oversized_graph.write_bytes(b" " * (MAX_GRAPH_BYTES + 1))
        oversized_certificate.write_bytes(b" " * (MAX_CERTIFICATE_BYTES + 1))

        for command, expected_label in (
            (("verify", str(oversized_graph)), "graph input"),
            (
                (
                    "replay",
                    str(oversized_certificate),
                    "--graph",
                    str(self.graph_path),
                ),
                "certificate input",
            ),
        ):
            with self.subTest(label=expected_label):
                exit_code, stdout, stderr = self.invoke(*command)
            self.assertEqual(exit_code, cli.EXIT_INPUT)
            self.assertEqual(stdout, "")
            self.assertEqual(
                stderr,
                f"unitsentinel: error: {expected_label} exceeds the byte limit\n",
            )

    def test_final_symlinks_directories_and_fifos_are_not_read(self) -> None:
        symlink = self.directory / "graph-link.json"
        directory = self.directory / "graph-directory"
        fifo = self.directory / "graph-fifo"
        symlink.symlink_to(self.graph_path)
        directory.mkdir()
        os.mkfifo(fifo)

        cases = (
            (symlink, "could not be opened"),
            (directory, "must be a regular file"),
            (fifo, "must be a regular file"),
        )
        for path, expected in cases:
            with self.subTest(kind=path.name):
                exit_code, stdout, stderr = self.invoke("verify", str(path))
            self.assertEqual(exit_code, cli.EXIT_INPUT)
            self.assertEqual(stdout, "")
            self.assertIn(expected, stderr)

    def test_read_failures_are_stable_and_close_the_descriptor(self) -> None:
        with patch.object(cli.os, "read", side_effect=OSError("private failure")):
            exit_code, stdout, stderr = self.invoke(
                "verify",
                str(self.graph_path),
            )

        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "unitsentinel: error: graph input could not be read\n",
        )
        self.assertNotIn("private", stderr)

    def test_stream_growth_is_capped_even_when_initial_size_is_stale(self) -> None:
        payload = self.directory / "growing-input"
        payload.write_bytes(b"1234")
        real_fstat = os.fstat

        def stale_size(descriptor: int) -> SimpleNamespace:
            metadata = real_fstat(descriptor)
            return SimpleNamespace(st_mode=metadata.st_mode, st_size=0)

        with (
            patch.object(cli.os, "fstat", side_effect=stale_size),
            self.assertRaises(cli._CLIError) as raised,
        ):
            cli._read_bounded_file(
                str(payload),
                label="test input",
                max_bytes=3,
            )

        self.assertEqual(raised.exception.exit_code, cli.EXIT_INPUT)
        self.assertEqual(
            raised.exception.message,
            "test input exceeds the byte limit",
        )


class AtomicOutputBoundaryTests(CLITestCase):
    def test_invalid_leaf_missing_parent_and_symlink_parent_fail_closed(self) -> None:
        real_directory = self.directory / "real"
        linked_directory = self.directory / "linked"
        real_directory.mkdir()
        linked_directory.symlink_to(real_directory, target_is_directory=True)
        cases = (
            (str(self.directory) + "/", "output path is invalid"),
            (
                str(self.directory / "missing" / "certificate.json"),
                "output directory could not be opened",
            ),
            (
                str(linked_directory / "certificate.json"),
                "output directory could not be opened",
            ),
        )
        for output, expected in cases:
            with self.subTest(expected=expected):
                exit_code, stdout, stderr = self.invoke(
                    "verify",
                    str(self.graph_path),
                    "--certificate",
                    output,
                )
            self.assertEqual(exit_code, cli.EXIT_INPUT)
            self.assertEqual(stdout, "")
            self.assertIn(expected, stderr)

    def test_existing_symlink_target_is_not_followed_or_replaced(self) -> None:
        owner_file = self.directory / "owner-file"
        output = self.directory / "certificate-link"
        owner_file.write_bytes(b"owner-data")
        output.symlink_to(owner_file)

        exit_code, stdout, stderr = self.invoke(
            "verify",
            str(self.graph_path),
            "--certificate",
            str(output),
        )

        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertIn("output already exists", stderr)
        self.assertTrue(output.is_symlink())
        self.assertEqual(owner_file.read_bytes(), b"owner-data")

    def test_write_link_and_directory_fsync_failures_never_publish_partial_bytes(
        self,
    ) -> None:
        write_output = self.directory / "write-failure.cert.json"
        with patch.object(cli, "_write_all", side_effect=OSError("write failure")):
            write_exit, write_stdout, write_stderr = self.invoke(
                "verify",
                str(self.graph_path),
                "--certificate",
                str(write_output),
            )
        self.assertEqual(write_exit, cli.EXIT_INPUT)
        self.assertEqual(write_stdout, "")
        self.assertIn("could not be written", write_stderr)
        self.assertFalse(write_output.exists())

        link_output = self.directory / "link-failure.cert.json"
        with patch.object(cli.os, "link", side_effect=OSError("link failure")):
            link_exit, link_stdout, link_stderr = self.invoke(
                "verify",
                str(self.graph_path),
                "--certificate",
                str(link_output),
            )
        self.assertEqual(link_exit, cli.EXIT_INPUT)
        self.assertEqual(link_stdout, "")
        self.assertIn("could not be published", link_stderr)
        self.assertFalse(link_output.exists())

        fsync_output = self.directory / "fsync-failure.cert.json"
        real_fsync = os.fsync

        def fail_directory_fsync(descriptor: int) -> None:
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("directory fsync failure")
            real_fsync(descriptor)

        with patch.object(cli.os, "fsync", side_effect=fail_directory_fsync):
            fsync_exit, fsync_stdout, fsync_stderr = self.invoke(
                "verify",
                str(self.graph_path),
                "--certificate",
                str(fsync_output),
            )
        self.assertEqual(fsync_exit, cli.EXIT_INPUT)
        self.assertEqual(fsync_stdout, "")
        self.assertIn("durability could not be confirmed", fsync_stderr)
        self.assertEqual(fsync_output.read_bytes(), self.certificate_bytes)
        self.assertEqual(list(self.directory.glob(".unitsentinel-*.tmp")), [])

    def test_postpublication_cleanup_failure_keeps_complete_target_and_errors(
        self,
    ) -> None:
        output = self.directory / "cleanup-failure.cert.json"
        with patch.object(cli.os, "unlink", side_effect=OSError("cleanup failure")):
            exit_code, stdout, stderr = self.invoke(
                "verify",
                str(self.graph_path),
                "--certificate",
                str(output),
            )

        self.assertEqual(exit_code, cli.EXIT_INPUT)
        self.assertEqual(stdout, "")
        self.assertIn("output cleanup could not be confirmed", stderr)
        self.assertEqual(output.read_bytes(), self.certificate_bytes)

    def test_temp_name_exhaustion_and_zero_length_write_fail_stably(self) -> None:
        collision = self.directory / ".unitsentinel-fixed.tmp"
        collision.write_bytes(b"owner-data")
        directory = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, directory)
        with (
            patch.object(cli.secrets, "token_hex", return_value="fixed"),
            self.assertRaises(cli._CLIError) as raised,
        ):
            cli._create_output_temp(directory)
        self.assertEqual(raised.exception.exit_code, cli.EXIT_INPUT)
        self.assertEqual(collision.read_bytes(), b"owner-data")

        with (
            patch.object(cli.os, "write", return_value=0),
            self.assertRaises(OSError),
        ):
            cli._write_all(directory, b"payload")


if __name__ == "__main__":
    unittest.main()
