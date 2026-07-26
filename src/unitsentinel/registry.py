"""Immutable, content-addressed unit registries.

Registry identifiers are deliberately separate from display symbols. Graphs
and certificates use canonical ASCII identifiers; symbols remain presentation
metadata and are never accepted as aliases implicitly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Final

from .domain import (
    AMOUNT_OF_SUBSTANCE,
    DIMENSIONLESS,
    ELECTRIC_CURRENT,
    LENGTH,
    LUMINOUS_INTENSITY,
    MASS,
    MAX_UNIT_ID_LENGTH,
    THERMODYNAMIC_TEMPERATURE,
    TIME,
    UNIT_ID,
    Dimension,
    QuantityKind,
    Unit,
    UnitSentinelError,
)

REGISTRY_SCHEMA: Final = "unitsentinel.unit-registry/v1"
MAX_REGISTRY_UNITS: Final = 64
MAX_REGISTRY_ALIASES: Final = 64
MAX_REGISTRY_VERSION_LENGTH: Final = 32
REGISTRY_VERSION: Final = re.compile(
    r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"
)
SHA256_HEX: Final = re.compile(r"^[0-9a-f]{64}$")


class RegistryError(UnitSentinelError):
    """Raised when a registry snapshot is malformed or has been mutated."""


class UnknownUnitError(RegistryError):
    """Raised when a canonical identifier is absent from a registry."""


def _require_registry_identifier(value: object, *, label: str) -> str:
    if (
        type(value) is not str
        or len(value) > MAX_UNIT_ID_LENGTH
        or UNIT_ID.fullmatch(value) is None
    ):
        raise RegistryError(f"{label} is not canonical")
    return value


@dataclass(frozen=True, slots=True)
class UnitAlias:
    """One explicit ASCII alias pointing directly to a canonical unit."""

    alias_id: str
    unit_id: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not UnitAlias:
            raise RegistryError("unit alias must be an exact UnitAlias")
        alias_id = _require_registry_identifier(self.alias_id, label="alias identifier")
        unit_id = _require_registry_identifier(self.unit_id, label="alias target")
        if alias_id == unit_id:
            raise RegistryError("unit alias cannot point to itself")

    def canonical_record(self) -> dict[str, str]:
        self.validate()
        return {"alias_id": self.alias_id, "unit_id": self.unit_id}


@dataclass(frozen=True, slots=True)
class UnitRegistry:
    """A bounded registry snapshot whose digest detects nested mutation."""

    version: str
    units: tuple[Unit, ...]
    aliases: tuple[UnitAlias, ...] = ()
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not UnitRegistry:
            raise RegistryError("registry must be an exact UnitRegistry")
        if (
            type(self.version) is not str
            or len(self.version) > MAX_REGISTRY_VERSION_LENGTH
            or REGISTRY_VERSION.fullmatch(self.version) is None
        ):
            raise RegistryError("registry version must be canonical SemVer")
        if type(self.units) is not tuple:
            raise RegistryError("registry units must be a tuple")
        if not self.units:
            raise RegistryError("registry must contain at least one unit")
        if len(self.units) > MAX_REGISTRY_UNITS:
            raise RegistryError("registry contains too many units")

        unit_ids: list[str] = []
        unit_symbols: list[str] = []
        for unit in self.units:
            if type(unit) is not Unit:
                raise RegistryError("registry entries must be exact Unit values")
            unit.validate()
            unit_ids.append(unit.unit_id)
            unit_symbols.append(unit.symbol)
        if len(set(unit_ids)) != len(unit_ids):
            raise RegistryError("registry unit identifiers must be unique")
        if len(set(unit_symbols)) != len(unit_symbols):
            raise RegistryError("registry unit symbols must be unique")
        if unit_ids != sorted(unit_ids):
            raise RegistryError("registry units must be sorted by identifier")

        if type(self.aliases) is not tuple:
            raise RegistryError("registry aliases must be a tuple")
        if len(self.aliases) > MAX_REGISTRY_ALIASES:
            raise RegistryError("registry contains too many aliases")

        alias_ids: list[str] = []
        canonical_ids = set(unit_ids)
        for alias in self.aliases:
            if type(alias) is not UnitAlias:
                raise RegistryError("registry aliases must be exact UnitAlias values")
            alias.validate()
            alias_ids.append(alias.alias_id)
            if alias.alias_id in canonical_ids:
                raise RegistryError("unit alias collides with a canonical identifier")
            if alias.unit_id not in canonical_ids:
                raise RegistryError("unit alias target is not in this registry")
        if len(set(alias_ids)) != len(alias_ids):
            raise RegistryError("unit alias identifiers must be unique")
        if alias_ids != sorted(alias_ids):
            raise RegistryError("unit aliases must be sorted by identifier")

    def validate(self) -> None:
        self._validate_structure()
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise RegistryError("registry digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise RegistryError("registry digest does not match its contents")

    def _canonical_record_unchecked(self) -> dict[str, object]:
        return {
            "aliases": [alias.canonical_record() for alias in self.aliases],
            "schema": REGISTRY_SCHEMA,
            "units": [unit.canonical_record() for unit in self.units],
            "version": self.version,
        }

    def _compute_digest(self) -> str:
        return hashlib.sha256(
            _canonical_json(self._canonical_record_unchecked())
        ).hexdigest()

    @property
    def digest(self) -> str:
        self.validate()
        return self._digest

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return self._canonical_record_unchecked()

    def canonical_bytes(self) -> bytes:
        self.validate()
        return _canonical_json(self._canonical_record_unchecked())

    def resolve(self, identifier: str) -> Unit:
        """Resolve one canonical identifier or explicit alias."""

        self.validate()
        lookup_id = _require_registry_identifier(identifier, label="unit lookup")
        for unit in self.units:
            if unit.unit_id == lookup_id:
                return unit
        for alias in self.aliases:
            if alias.alias_id == lookup_id:
                return self._resolve_canonical(alias.unit_id)
        raise UnknownUnitError("unit identifier is not present in this registry")

    def _resolve_canonical(self, unit_id: str) -> Unit:
        for unit in self.units:
            if unit.unit_id == unit_id:
                return unit
        raise RegistryError("validated alias target disappeared")

    def conversion_targets(self, identifier: str) -> tuple[Unit, ...]:
        """Return deterministic same-dimension, same-kind conversion targets."""

        source = self.resolve(identifier)
        return tuple(
            unit
            for unit in self.units
            if unit.dimension == source.dimension and unit.kind is source.kind
        )


def _canonical_json(record: dict[str, object]) -> bytes:
    return json.dumps(
        record,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _linear(
    unit_id: str,
    symbol: str,
    dimension: Dimension,
    scale: Fraction = Fraction(1),
) -> Unit:
    return Unit(unit_id, symbol, dimension, scale)


def _temperature(
    unit_id: str,
    symbol: str,
    scale: Fraction,
    offset: Fraction,
    kind: QuantityKind,
) -> Unit:
    return Unit(
        unit_id,
        symbol,
        THERMODYNAMIC_TEMPERATURE,
        scale,
        offset,
        kind,
    )


def _builtin_registry() -> UnitRegistry:
    velocity = LENGTH.divide(TIME)
    acceleration = velocity.divide(TIME)
    force = MASS.multiply(acceleration)
    energy = force.multiply(LENGTH)
    power = energy.divide(TIME)

    units = (
        _linear("ampere", "A", ELECTRIC_CURRENT),
        _linear("candela", "cd", LUMINOUS_INTENSITY),
        _linear("centimeter", "cm", LENGTH, Fraction(1, 100)),
        _linear("coulomb", "C", ELECTRIC_CURRENT.multiply(TIME)),
        _temperature(
            "degree-celsius",
            "°C",
            Fraction(1),
            Fraction(27_315, 100),
            QuantityKind.ABSOLUTE_TEMPERATURE,
        ),
        _temperature(
            "degree-fahrenheit",
            "°F",
            Fraction(5, 9),
            Fraction(45_967, 180),
            QuantityKind.ABSOLUTE_TEMPERATURE,
        ),
        _temperature(
            "delta-celsius",
            "Δ°C",
            Fraction(1),
            Fraction(0),
            QuantityKind.TEMPERATURE_DELTA,
        ),
        _temperature(
            "delta-fahrenheit",
            "Δ°F",
            Fraction(5, 9),
            Fraction(0),
            QuantityKind.TEMPERATURE_DELTA,
        ),
        _temperature(
            "delta-kelvin",
            "ΔK",
            Fraction(1),
            Fraction(0),
            QuantityKind.TEMPERATURE_DELTA,
        ),
        _linear("gram", "g", MASS, Fraction(1, 1_000)),
        _linear("hertz", "Hz", TIME.power(Fraction(-1))),
        _linear("hour", "h", TIME, Fraction(3_600)),
        _linear("joule", "J", energy),
        _temperature(
            "kelvin",
            "K",
            Fraction(1),
            Fraction(0),
            QuantityKind.ABSOLUTE_TEMPERATURE,
        ),
        _linear("kilogram", "kg", MASS),
        _linear("kilometer", "km", LENGTH, Fraction(1_000)),
        _linear("kilometer-per-hour", "km/h", velocity, Fraction(5, 18)),
        _linear("meter", "m", LENGTH),
        _linear("meter-per-second", "m/s", velocity),
        _linear("meter-per-second-squared", "m/s²", acceleration),
        _linear("micrometer", "µm", LENGTH, Fraction(1, 1_000_000)),
        _linear("milligram", "mg", MASS, Fraction(1, 1_000_000)),
        _linear("millimeter", "mm", LENGTH, Fraction(1, 1_000)),
        _linear("millisecond", "ms", TIME, Fraction(1, 1_000)),
        _linear("minute", "min", TIME, Fraction(60)),
        _linear("mole", "mol", AMOUNT_OF_SUBSTANCE),
        _linear("newton", "N", force),
        _linear("one", "1", DIMENSIONLESS),
        _linear("pascal", "Pa", force.divide(LENGTH.power(Fraction(2)))),
        _linear("percent", "%", DIMENSIONLESS, Fraction(1, 100)),
        _linear("second", "s", TIME),
        _linear("volt", "V", power.divide(ELECTRIC_CURRENT)),
        _linear("watt", "W", power),
    )
    aliases = (
        UnitAlias("celsius", "degree-celsius"),
        UnitAlias("celsius-delta", "delta-celsius"),
        UnitAlias("centimetre", "centimeter"),
        UnitAlias("fahrenheit", "degree-fahrenheit"),
        UnitAlias("fahrenheit-delta", "delta-fahrenheit"),
        UnitAlias("kelvin-delta", "delta-kelvin"),
        UnitAlias("kilometre", "kilometer"),
        UnitAlias("metre", "meter"),
        UnitAlias("micrometre", "micrometer"),
        UnitAlias("millimetre", "millimeter"),
    )
    return UnitRegistry(version="1.0.0", units=units, aliases=aliases)


BUILTIN_REGISTRY: Final = _builtin_registry()
