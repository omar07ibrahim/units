"""Exact dimensional and unit value objects.

The module deliberately accepts ``Fraction`` rather than floats. Unit contracts
are metadata, so preserving an exact scale is more useful than accepting every
numeric convenience type.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Final

BASE_DIMENSION_COUNT: Final = 7
MAX_EXPONENT_NUMERATOR: Final = 64
MAX_EXPONENT_DENOMINATOR: Final = 12
MAX_RATIONAL_BITS: Final = 256
MAX_UNIT_ID_LENGTH: Final = 64
MAX_UNIT_SYMBOL_LENGTH: Final = 24
UNIT_ID: Final = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


class UnitSentinelError(ValueError):
    """Base class for stable, user-facing domain errors."""


class DimensionError(UnitSentinelError):
    """Raised when a dimension is malformed or exceeds declared bounds."""


class UnitDefinitionError(UnitSentinelError):
    """Raised when a concrete unit definition is invalid."""


class ConversionError(UnitSentinelError):
    """Raised when an exact conversion is not semantically valid."""


class BaseDimension(StrEnum):
    """The seven SI base dimensions in certificate order."""

    LENGTH = "length"
    MASS = "mass"
    TIME = "time"
    ELECTRIC_CURRENT = "electric-current"
    THERMODYNAMIC_TEMPERATURE = "thermodynamic-temperature"
    AMOUNT_OF_SUBSTANCE = "amount-of-substance"
    LUMINOUS_INTENSITY = "luminous-intensity"


BASE_DIMENSIONS: Final = tuple(BaseDimension)


class QuantityKind(StrEnum):
    """Semantics that cannot be recovered from a dimension vector alone."""

    LINEAR = "linear"
    ABSOLUTE_TEMPERATURE = "absolute-temperature"
    TEMPERATURE_DELTA = "temperature-delta"


def _require_exact_fraction(
    value: object,
    *,
    label: str,
    error_type: type[UnitSentinelError],
) -> Fraction:
    if type(value) is not Fraction:
        raise error_type(f"{label} must be an exact Fraction")
    if (
        abs(value.numerator).bit_length() > MAX_RATIONAL_BITS
        or value.denominator.bit_length() > MAX_RATIONAL_BITS
    ):
        raise error_type(f"{label} exceeds the rational size limit")
    return value


def _validate_exponent(value: object) -> Fraction:
    exponent = _require_exact_fraction(
        value,
        label="dimension exponent",
        error_type=DimensionError,
    )
    if (
        abs(exponent.numerator) > MAX_EXPONENT_NUMERATOR
        or exponent.denominator > MAX_EXPONENT_DENOMINATOR
    ):
        raise DimensionError("dimension exponent exceeds the declared bounds")
    return exponent


@dataclass(frozen=True, slots=True)
class Dimension:
    """An immutable exact vector over the seven SI base dimensions."""

    exponents: tuple[Fraction, ...]

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not Dimension:
            raise DimensionError("dimension must be an exact Dimension")
        if type(self.exponents) is not tuple:
            raise DimensionError("dimension exponents must be a tuple")
        if len(self.exponents) != BASE_DIMENSION_COUNT:
            raise DimensionError("dimension must contain seven exponents")
        for exponent in self.exponents:
            _validate_exponent(exponent)

    @classmethod
    def dimensionless(cls) -> Dimension:
        return cls((Fraction(0),) * BASE_DIMENSION_COUNT)

    @classmethod
    def base(cls, base: BaseDimension) -> Dimension:
        if type(base) is not BaseDimension:
            raise DimensionError("base dimension must be a known SI dimension")
        values = [Fraction(0)] * BASE_DIMENSION_COUNT
        values[BASE_DIMENSIONS.index(base)] = Fraction(1)
        return cls(tuple(values))

    @classmethod
    def from_mapping(
        cls,
        exponents: Mapping[BaseDimension, Fraction],
    ) -> Dimension:
        if not isinstance(exponents, Mapping):
            raise DimensionError("dimension mapping must implement Mapping")
        if len(exponents) > BASE_DIMENSION_COUNT:
            raise DimensionError("dimension mapping contains too many entries")
        if any(type(key) is not BaseDimension for key in exponents):
            raise DimensionError("dimension mapping contains an unknown base")
        return cls(
            tuple(
                _validate_exponent(exponents.get(base, Fraction(0)))
                for base in BASE_DIMENSIONS
            )
        )

    def multiply(self, other: Dimension) -> Dimension:
        self.validate()
        _require_dimension(other)
        return Dimension(
            tuple(
                left + right
                for left, right in zip(
                    self.exponents,
                    other.exponents,
                    strict=True,
                )
            )
        )

    def divide(self, other: Dimension) -> Dimension:
        self.validate()
        _require_dimension(other)
        return Dimension(
            tuple(
                left - right
                for left, right in zip(
                    self.exponents,
                    other.exponents,
                    strict=True,
                )
            )
        )

    def power(self, exponent: Fraction) -> Dimension:
        self.validate()
        factor = _validate_exponent(exponent)
        return Dimension(tuple(value * factor for value in self.exponents))

    @property
    def is_dimensionless(self) -> bool:
        self.validate()
        return all(exponent == 0 for exponent in self.exponents)

    def canonical_pairs(self) -> tuple[tuple[str, str], ...]:
        """Return the stable non-zero representation used by codecs."""

        self.validate()
        return tuple(
            (base.value, _fraction_text(exponent))
            for base, exponent in zip(
                BASE_DIMENSIONS,
                self.exponents,
                strict=True,
            )
            if exponent
        )


def _require_dimension(value: object) -> Dimension:
    if type(value) is not Dimension:
        raise DimensionError("operand must be an exact Dimension")
    value.validate()
    return value


def _fraction_text(value: Fraction) -> str:
    return (
        str(value.numerator)
        if value.denominator == 1
        else f"{value.numerator}/{value.denominator}"
    )


def _validate_display_symbol(symbol: object) -> str:
    if type(symbol) is not str:
        raise UnitDefinitionError("unit symbol must be text")
    if not symbol or symbol != symbol.strip() or len(symbol) > MAX_UNIT_SYMBOL_LENGTH:
        raise UnitDefinitionError("unit symbol has an invalid length or padding")
    if any(character.isspace() for character in symbol):
        raise UnitDefinitionError("unit symbol contains whitespace")
    normalized = unicodedata.normalize("NFC", symbol)
    if normalized != symbol:
        raise UnitDefinitionError("unit symbol must use canonical Unicode")
    if any(unicodedata.category(character).startswith("C") for character in symbol):
        raise UnitDefinitionError("unit symbol contains a control character")
    return symbol


@dataclass(frozen=True, slots=True)
class Unit:
    """A concrete exact transform to one dimension's reference unit."""

    unit_id: str
    symbol: str
    dimension: Dimension
    scale: Fraction
    offset: Fraction = Fraction(0)
    kind: QuantityKind = QuantityKind.LINEAR

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not Unit:
            raise UnitDefinitionError("unit must be an exact Unit")
        if (
            type(self.unit_id) is not str
            or len(self.unit_id) > MAX_UNIT_ID_LENGTH
            or UNIT_ID.fullmatch(self.unit_id) is None
        ):
            raise UnitDefinitionError("unit identifier is not canonical")
        _validate_display_symbol(self.symbol)
        dimension = _require_dimension(self.dimension)
        scale = _require_exact_fraction(
            self.scale,
            label="unit scale",
            error_type=UnitDefinitionError,
        )
        offset = _require_exact_fraction(
            self.offset,
            label="unit offset",
            error_type=UnitDefinitionError,
        )
        if scale <= 0:
            raise UnitDefinitionError("unit scale must be positive")
        if type(self.kind) is not QuantityKind:
            raise UnitDefinitionError("unit kind is unknown")
        if self.kind is QuantityKind.LINEAR:
            if dimension == THERMODYNAMIC_TEMPERATURE:
                raise UnitDefinitionError(
                    "temperature units require an explicit absolute or delta kind"
                )
            if offset:
                raise UnitDefinitionError("linear units cannot have an offset")
            return
        if dimension != THERMODYNAMIC_TEMPERATURE:
            raise UnitDefinitionError("temperature kinds require temperature dimension")
        if self.kind is QuantityKind.TEMPERATURE_DELTA and offset:
            raise UnitDefinitionError("temperature deltas cannot have an offset")

    def convert_value_to(self, value: Fraction, target: Unit) -> Fraction:
        """Convert one exact value while preserving quantity-kind semantics."""

        self.validate()
        if type(target) is not Unit:
            raise ConversionError("conversion target must be an exact Unit")
        target.validate()
        magnitude = _require_exact_fraction(
            value,
            label="conversion value",
            error_type=ConversionError,
        )
        if self.dimension != target.dimension:
            raise ConversionError("units have incompatible dimensions")
        if self.kind is not target.kind:
            raise ConversionError("units have incompatible quantity kinds")

        reference = magnitude * self.scale
        if self.kind is QuantityKind.ABSOLUTE_TEMPERATURE:
            reference += self.offset
            converted = (reference - target.offset) / target.scale
        else:
            converted = reference / target.scale
        return _require_exact_fraction(
            converted,
            label="converted value",
            error_type=ConversionError,
        )

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return {
            "dimension": [
                {"base": base, "exponent": exponent}
                for base, exponent in self.dimension.canonical_pairs()
            ],
            "kind": self.kind.value,
            "offset": _fraction_text(self.offset),
            "scale": _fraction_text(self.scale),
            "symbol": self.symbol,
            "unit_id": self.unit_id,
        }


@dataclass(frozen=True, slots=True)
class Quantity:
    """An exact magnitude paired with one validated concrete unit."""

    magnitude: Fraction
    unit: Unit

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not Quantity:
            raise UnitDefinitionError("quantity must be an exact Quantity")
        _require_exact_fraction(
            self.magnitude,
            label="quantity magnitude",
            error_type=UnitDefinitionError,
        )
        if type(self.unit) is not Unit:
            raise UnitDefinitionError("quantity unit must be an exact Unit")
        self.unit.validate()

    def to(self, target: Unit) -> Quantity:
        self.validate()
        return Quantity(self.unit.convert_value_to(self.magnitude, target), target)

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return {
            "magnitude": _fraction_text(self.magnitude),
            "unit_id": self.unit.unit_id,
        }


DIMENSIONLESS: Final = Dimension.dimensionless()
LENGTH: Final = Dimension.base(BaseDimension.LENGTH)
MASS: Final = Dimension.base(BaseDimension.MASS)
TIME: Final = Dimension.base(BaseDimension.TIME)
ELECTRIC_CURRENT: Final = Dimension.base(BaseDimension.ELECTRIC_CURRENT)
THERMODYNAMIC_TEMPERATURE: Final = Dimension.base(
    BaseDimension.THERMODYNAMIC_TEMPERATURE
)
AMOUNT_OF_SUBSTANCE: Final = Dimension.base(BaseDimension.AMOUNT_OF_SUBSTANCE)
LUMINOUS_INTENSITY: Final = Dimension.base(BaseDimension.LUMINOUS_INTENSITY)
