from __future__ import annotations

import unittest
from importlib.metadata import version

import unitsentinel


class PackageIdentityTests(unittest.TestCase):
    def test_version_and_public_surface_are_explicit(self) -> None:
        self.assertEqual(unitsentinel.__version__, "0.1.0")
        self.assertEqual(version("unitsentinel"), unitsentinel.__version__)
        self.assertEqual(
            unitsentinel.__all__,
            [
                "AMOUNT_OF_SUBSTANCE",
                "BUILTIN_REGISTRY",
                "DIMENSIONLESS",
                "ELECTRIC_CURRENT",
                "LENGTH",
                "LUMINOUS_INTENSITY",
                "MASS",
                "THERMODYNAMIC_TEMPERATURE",
                "TIME",
                "BaseDimension",
                "CertificateDecodeError",
                "CertificateError",
                "CertificateReplay",
                "CertificateReplayError",
                "ComputationGraph",
                "ConstraintSource",
                "ConstraintWitness",
                "ConversionError",
                "Dimension",
                "DimensionError",
                "GraphDecodeError",
                "GraphError",
                "GraphValidationError",
                "InferredContract",
                "Node",
                "Operation",
                "ProofCertificate",
                "Quantity",
                "QuantityKind",
                "RegistryError",
                "ReplayReason",
                "ReplayStatus",
                "ScalarType",
                "SolverLimits",
                "Unit",
                "UnitAlias",
                "UnitDefinitionError",
                "UnitRegistry",
                "UnitSentinelError",
                "UnknownReason",
                "UnknownUnitError",
                "ValueSpec",
                "VerificationError",
                "VerificationResult",
                "VerificationStatus",
                "__version__",
                "constraint_catalog",
                "create_certificate",
                "decode_certificate",
                "decode_graph",
                "encode_certificate",
                "encode_graph",
                "replay_certificate",
                "verify_graph",
            ],
        )


if __name__ == "__main__":
    unittest.main()
