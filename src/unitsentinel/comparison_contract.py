"""Immutable explicit bindings for training-versus-serving comparisons.

This layer records which declared graph interfaces a later comparison engine
must inspect.  It deliberately performs no name matching and makes no claim
that paired roles or unit contracts agree.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from .canonical import canonical_json_bytes, sha256_hex
from .domain import MAX_UNIT_ID_LENGTH, UNIT_ID, UnitSentinelError
from .registry import SHA256_HEX

COMPARISON_SCHEMA: Final = "unitsentinel.training-serving-comparison/v1"
MAX_COMPARISON_BINDINGS: Final = 256
MAX_COMPARISON_ID_LENGTH: Final = 64
MAX_CONTRACT_ID_LENGTH: Final = 64


class ComparisonContractError(UnitSentinelError):
    """Base class for stable training-serving comparison-contract failures."""


class ComparisonValidationError(ComparisonContractError):
    """Raised when an immutable comparison value violates the v1 contract."""


class InterfaceRole(StrEnum):
    """A closed role at one graph's public interface."""

    INPUT = "input"
    OUTPUT = "output"


def _require_identifier(
    value: object,
    *,
    label: str,
    max_length: int = MAX_UNIT_ID_LENGTH,
) -> str:
    if (
        type(value) is not str
        or len(value) > max_length
        or UNIT_ID.fullmatch(value) is None
    ):
        raise ComparisonValidationError(f"{label} is not canonical")
    return value


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or SHA256_HEX.fullmatch(value) is None:
        raise ComparisonValidationError(f"{label} is malformed")
    return value


@dataclass(frozen=True, slots=True)
class InterfaceEndpoint:
    """One explicitly named input or output of a graph."""

    role: InterfaceRole
    value_id: str

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not InterfaceEndpoint:
            raise ComparisonValidationError(
                "interface endpoint must be an exact InterfaceEndpoint"
            )
        if type(self.role) is not InterfaceRole:
            raise ComparisonValidationError("interface endpoint role is unsupported")
        _require_identifier(self.value_id, label="interface value identifier")

    def canonical_record(self) -> dict[str, str]:
        self.validate()
        return {
            "role": self.role.value,
            "value_id": self.value_id,
        }


@dataclass(frozen=True, slots=True)
class ContractBinding:
    """An explicit, possibly one-sided interface mapping under one stable ID."""

    contract_id: str
    training: InterfaceEndpoint | None
    serving: InterfaceEndpoint | None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self) is not ContractBinding:
            raise ComparisonValidationError(
                "contract binding must be an exact ContractBinding"
            )
        _require_identifier(
            self.contract_id,
            label="contract identifier",
            max_length=MAX_CONTRACT_ID_LENGTH,
        )
        if self.training is None and self.serving is None:
            raise ComparisonValidationError(
                "contract binding must declare a training or serving endpoint"
            )
        for side, endpoint in (
            ("training", self.training),
            ("serving", self.serving),
        ):
            if endpoint is None:
                continue
            if type(endpoint) is not InterfaceEndpoint:
                raise ComparisonValidationError(
                    f"{side} endpoint must be an exact InterfaceEndpoint or null"
                )
            endpoint.validate()

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return {
            "contract_id": self.contract_id,
            "serving": (
                None if self.serving is None else self.serving.canonical_record()
            ),
            "training": (
                None if self.training is None else self.training.canonical_record()
            ),
        }


@dataclass(frozen=True, slots=True)
class ComparisonPlan:
    """A bounded, content-addressed plan containing only explicit mappings."""

    comparison_id: str
    training_graph_digest: str
    serving_graph_digest: str
    registry_digest: str
    bindings: tuple[ContractBinding, ...]
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not ComparisonPlan:
            raise ComparisonValidationError(
                "comparison plan must be an exact ComparisonPlan"
            )
        _require_identifier(
            self.comparison_id,
            label="comparison identifier",
            max_length=MAX_COMPARISON_ID_LENGTH,
        )
        _require_digest(
            self.training_graph_digest,
            label="training graph digest",
        )
        _require_digest(
            self.serving_graph_digest,
            label="serving graph digest",
        )
        _require_digest(self.registry_digest, label="registry digest")
        self._validate_bindings()

    def _validate_bindings(self) -> None:
        if type(self.bindings) is not tuple:
            raise ComparisonValidationError("comparison bindings must be a tuple")
        if not self.bindings:
            raise ComparisonValidationError(
                "comparison plan must declare at least one binding"
            )
        if len(self.bindings) > MAX_COMPARISON_BINDINGS:
            raise ComparisonValidationError(
                "comparison plan contains too many bindings"
            )

        contract_ids: list[str] = []
        training_endpoints: set[tuple[InterfaceRole, str]] = set()
        serving_endpoints: set[tuple[InterfaceRole, str]] = set()
        for binding in self.bindings:
            if type(binding) is not ContractBinding:
                raise ComparisonValidationError(
                    "comparison bindings must be exact ContractBinding instances"
                )
            binding.validate()
            contract_ids.append(binding.contract_id)
            self._record_endpoint(
                binding.training,
                seen=training_endpoints,
                side="training",
            )
            self._record_endpoint(
                binding.serving,
                seen=serving_endpoints,
                side="serving",
            )

        if len(set(contract_ids)) != len(contract_ids):
            raise ComparisonValidationError(
                "comparison contract identifiers must be unique"
            )
        if contract_ids != sorted(contract_ids):
            raise ComparisonValidationError(
                "comparison bindings must be sorted by contract identifier"
            )

    @staticmethod
    def _record_endpoint(
        endpoint: InterfaceEndpoint | None,
        *,
        seen: set[tuple[InterfaceRole, str]],
        side: str,
    ) -> None:
        if endpoint is None:
            return
        key = (endpoint.role, endpoint.value_id)
        if key in seen:
            raise ComparisonValidationError(
                f"{side} interface endpoints must occur at most once"
            )
        seen.add(key)

    def validate(self) -> None:
        self._validate_structure()
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise ComparisonValidationError("comparison plan digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise ComparisonValidationError(
                "comparison plan digest does not match its contents"
            )

    def _canonical_record_unchecked(self) -> dict[str, object]:
        return {
            "bindings": [binding.canonical_record() for binding in self.bindings],
            "comparison_id": self.comparison_id,
            "registry_digest": self.registry_digest,
            "schema": COMPARISON_SCHEMA,
            "serving_graph_digest": self.serving_graph_digest,
            "training_graph_digest": self.training_graph_digest,
        }

    def _compute_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self._canonical_record_unchecked()))

    @property
    def digest(self) -> str:
        self.validate()
        return self._digest

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return self._canonical_record_unchecked()

    def canonical_bytes(self) -> bytes:
        self.validate()
        return canonical_json_bytes(self._canonical_record_unchecked())
