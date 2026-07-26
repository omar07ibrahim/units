"""Exact dimensional contracts for scientific and ML computation graphs."""

from typing import Final

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
from .registry import (
    BUILTIN_REGISTRY,
    RegistryError,
    UnitAlias,
    UnitRegistry,
    UnknownUnitError,
)

__version__: Final = "0.1.0"

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
    "ConversionError",
    "Dimension",
    "DimensionError",
    "Quantity",
    "QuantityKind",
    "RegistryError",
    "Unit",
    "UnitAlias",
    "UnitDefinitionError",
    "UnitRegistry",
    "UnitSentinelError",
    "UnknownUnitError",
    "__version__",
]
