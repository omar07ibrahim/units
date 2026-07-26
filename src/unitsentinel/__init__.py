"""Exact dimensional contracts for scientific and ML computation graphs."""

from typing import Final

from .certificate import (
    CertificateError,
    ProofCertificate,
    create_certificate,
    encode_certificate,
)
from .domain import (
    AMOUNT_OF_SUBSTANCE,
    DIMENSIONLESS,
    ELECTRIC_CURRENT,
    LENGTH,
    LUMINOUS_INTENSITY,
    MASS,
    THERMODYNAMIC_TEMPERATURE,
    TIME,
    BaseDimension,
    ConversionError,
    Dimension,
    DimensionError,
    Quantity,
    QuantityKind,
    Unit,
    UnitDefinitionError,
    UnitSentinelError,
)
from .graph import (
    ComputationGraph,
    GraphError,
    GraphValidationError,
    Node,
    Operation,
    ScalarType,
    ValueSpec,
)
from .graph_codec import GraphDecodeError, decode_graph, encode_graph
from .registry import (
    BUILTIN_REGISTRY,
    RegistryError,
    UnitAlias,
    UnitRegistry,
    UnknownUnitError,
)
from .verification import (
    ConstraintSource,
    ConstraintWitness,
    InferredContract,
    SolverLimits,
    UnknownReason,
    VerificationError,
    VerificationResult,
    VerificationStatus,
)
from .verifier import constraint_catalog, verify_graph
from .version import VERSION

__version__: Final = VERSION

__all__ = [
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
    "CertificateError",
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
    "decode_graph",
    "encode_certificate",
    "encode_graph",
    "verify_graph",
]
