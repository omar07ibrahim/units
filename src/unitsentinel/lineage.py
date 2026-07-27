"""Bounded semantic lineage for dimensionless ratio normalization.

The extractor builds one iterative, content-addressed DAG. Semantic hashes
exclude graph-local identifiers, while independently content-addressed
diagnostic records retain them for review.
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from typing import Final

import z3  # type: ignore[import-untyped]

from .canonical import canonical_json_bytes, sha256_hex
from .comparison import ComparisonPolicy
from .comparison_contract import (
    MAX_COMPARISON_ID_LENGTH,
    MAX_CONTRACT_ID_LENGTH,
    ComparisonPlan,
    InterfaceRole,
)
from .domain import (
    DIMENSIONLESS,
    MAX_EXPONENT_DENOMINATOR,
    MAX_EXPONENT_NUMERATOR,
    MAX_UNIT_ID_LENGTH,
    UNIT_ID,
    QuantityKind,
    UnitSentinelError,
)
from .graph import (
    BINARY_OPERATIONS,
    MAX_GRAPH_INPUTS,
    MAX_GRAPH_NODES,
    MAX_GRAPH_OUTPUTS,
    MAX_GRAPH_VALUES,
    ComputationGraph,
    Node,
    Operation,
    ValueSpec,
)
from .registry import BUILTIN_REGISTRY, SHA256_HEX, UnitRegistry
from .verification import (
    SOLVER_VERSION,
    InferredContract,
    SolverLimits,
    VerificationResult,
    VerificationStatus,
)
from .verifier import _replay_claimed_contracts

NORMALIZATION_LINEAGE_SCHEMA: Final = "unitsentinel.normalization-lineage/v1"
NORMALIZATION_LINEAGE_SEMANTIC_SCHEMA: Final = (
    "unitsentinel.normalization-lineage-semantic/v1"
)
NORMALIZATION_EXPRESSION_SCHEMA: Final = "unitsentinel.normalization-expression/v1"
NORMALIZATION_SITE_SCHEMA: Final = "unitsentinel.normalization-site/v1"
LINEAGE_AUTHENTICATION: Final = "not-provided"
_DEFAULT_LINEAGE_LIMITS: Final = SolverLimits()
_DEFAULT_LINEAGE_POLICY: Final = ComparisonPolicy()
_COMMUTATIVE_OPERATIONS: Final = frozenset(
    {
        Operation.ADD,
        Operation.MULTIPLY,
        Operation.MINIMUM,
        Operation.MAXIMUM,
    }
)
_FRACTION_TEXT: Final = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:/[1-9][0-9]*)?$")
_MAX_POWER_ATTRIBUTE_LENGTH: Final = len(
    f"-{MAX_EXPONENT_NUMERATOR}/{MAX_EXPONENT_DENOMINATOR}"
)


class LineageError(UnitSentinelError):
    """Raised when lineage inputs or immutable records fail closed."""


class LineageSide(StrEnum):
    """The selected side of one explicit comparison plan."""

    TRAINING = "training"
    SERVING = "serving"


def _require_digest(value: object, *, label: str) -> str:
    if type(value) is not str or SHA256_HEX.fullmatch(value) is None:
        raise LineageError(f"{label} is malformed")
    return value


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
        raise LineageError(f"{label} is not canonical")
    return value


def _fraction_text(value: Fraction) -> str:
    return (
        str(value.numerator)
        if value.denominator == 1
        else f"{value.numerator}/{value.denominator}"
    )


def _semantic_metadata(
    value: ValueSpec,
    inferred: InferredContract,
) -> dict[str, object]:
    value.validate()
    inferred.validate()
    inferred_record = inferred.canonical_record()
    inferred_record.pop("value_id")
    return {
        "dtype": value.dtype.value,
        "inferred": inferred_record,
        "shape": list(value.shape),
        "unit_id": value.unit_id,
    }


def _metadata_equal(
    left_value: ValueSpec,
    left_inferred: InferredContract,
    right_value: ValueSpec,
    right_inferred: InferredContract,
) -> bool:
    return _semantic_metadata(left_value, left_inferred) == _semantic_metadata(
        right_value,
        right_inferred,
    )


def _digest_multiset_record(digests: tuple[str, ...]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    index = 0
    while index < len(digests):
        digest = digests[index]
        end = index + 1
        while end < len(digests) and digests[end] == digest:
            end += 1
        records.append({"count": end - index, "sha256": digest})
        index = end
    return records


def _validate_attributes(
    operation: Operation,
    attributes: tuple[tuple[str, str], ...],
) -> None:
    if type(attributes) is not tuple:
        raise LineageError("lineage expression attributes must be a tuple")
    if any(
        type(item) is not tuple
        or len(item) != 2
        or type(item[0]) is not str
        or type(item[1]) is not str
        for item in attributes
    ):
        raise LineageError("lineage expression attributes are malformed")
    if list(attributes) != sorted(set(attributes)):
        raise LineageError("lineage expression attributes must be sorted and unique")
    if operation is Operation.POWER:
        if len(attributes) != 1 or attributes[0][0] != "exponent":
            raise LineageError("power lineage attributes are inconsistent")
        text = attributes[0][1]
        if len(text) > _MAX_POWER_ATTRIBUTE_LENGTH:
            raise LineageError("power lineage exponent is too long")
        if _FRACTION_TEXT.fullmatch(text) is None:
            raise LineageError("power lineage exponent is not canonical")
        exponent = Fraction(text)
        if text != _fraction_text(exponent):
            raise LineageError("power lineage exponent is not canonical")
        if (
            abs(exponent.numerator) > MAX_EXPONENT_NUMERATOR
            or exponent.denominator > MAX_EXPONENT_DENOMINATOR
        ):
            raise LineageError("power lineage exponent is out of bounds")
        return
    if operation is Operation.CONVERT:
        if len(attributes) != 1 or attributes[0][0] != "unit_id":
            raise LineageError("conversion lineage attributes are inconsistent")
        _require_identifier(
            attributes[0][1],
            label="conversion lineage unit identifier",
        )
        return
    if attributes:
        raise LineageError("operation does not accept lineage attributes")


@dataclass(frozen=True, slots=True)
class LineageExpression:
    """One diagnostic graph value backed by an identifier-free semantic hash."""

    value_id: str
    node_id: str | None
    operation: Operation | None
    attributes: tuple[tuple[str, str], ...]
    input_value_ids: tuple[str, ...]
    child_digests: tuple[str, ...]
    logical_roots: tuple[str, ...]
    collapsed_identity: bool
    value: ValueSpec
    inferred: InferredContract
    _semantic_digest: str = field(init=False, repr=False)
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(
            self,
            "_semantic_digest",
            self._compute_semantic_digest(),
        )
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not LineageExpression:
            raise LineageError("lineage expression must be an exact LineageExpression")
        _require_identifier(self.value_id, label="lineage value identifier")
        if type(self.value) is not ValueSpec:
            raise LineageError("lineage value must be an exact ValueSpec")
        if type(self.inferred) is not InferredContract:
            raise LineageError("lineage contract must be an exact InferredContract")
        try:
            self.value.validate()
            self.inferred.validate()
        except UnitSentinelError:
            raise LineageError(
                "lineage expression contains malformed or mutated metadata"
            ) from None
        if (
            self.value.value_id != self.value_id
            or self.inferred.value_id != self.value_id
        ):
            raise LineageError("lineage expression value identities are inconsistent")
        if type(self.logical_roots) is not tuple:
            raise LineageError("lineage logical roots must be a tuple")
        for root_id in self.logical_roots:
            _require_identifier(
                root_id,
                label="lineage logical root",
                max_length=MAX_CONTRACT_ID_LENGTH,
            )
        if len(self.logical_roots) > MAX_GRAPH_INPUTS:
            raise LineageError("lineage logical roots exceed the graph input limit")
        if not self.logical_roots or list(self.logical_roots) != sorted(
            set(self.logical_roots)
        ):
            raise LineageError("lineage logical roots must be nonempty and sorted")
        if type(self.collapsed_identity) is not bool:
            raise LineageError("collapsed-identity flag must be an exact boolean")
        self._validate_source_shape()

    def _validate_source_shape(self) -> None:
        if type(self.input_value_ids) is not tuple:
            raise LineageError("lineage input identifiers must be a tuple")
        for input_id in self.input_value_ids:
            _require_identifier(input_id, label="lineage input identifier")
        if type(self.child_digests) is not tuple:
            raise LineageError("lineage child digests must be a tuple")
        for digest in self.child_digests:
            _require_digest(digest, label="lineage child digest")
        if len(self.child_digests) != len(self.input_value_ids):
            raise LineageError("lineage children and diagnostics are inconsistent")

        if self.operation is None:
            if (
                self.node_id is not None
                or self.attributes
                or self.input_value_ids
                or self.child_digests
                or self.collapsed_identity
                or len(self.logical_roots) != 1
            ):
                raise LineageError("lineage input expression fields are inconsistent")
            return
        if type(self.operation) is not Operation:
            raise LineageError("lineage operation is unsupported")
        _require_identifier(self.node_id, label="lineage node identifier")
        expected_arity = 2 if self.operation in BINARY_OPERATIONS else 1
        if len(self.input_value_ids) != expected_arity:
            raise LineageError("lineage operation arity is inconsistent")
        _validate_attributes(self.operation, self.attributes)
        if self.operation in _COMMUTATIVE_OPERATIONS and list(
            self.child_digests
        ) != sorted(self.child_digests):
            raise LineageError("commutative lineage children must be sorted")
        if self.collapsed_identity and self.operation is not Operation.IDENTITY:
            raise LineageError("only identity expressions may be collapsed")

    def _semantic_record_unchecked(self) -> dict[str, object]:
        if self.operation is None:
            return {
                "kind": "input",
                "logical_contract_id": self.logical_roots[0],
                "schema": NORMALIZATION_EXPRESSION_SCHEMA,
                "value": _semantic_metadata(self.value, self.inferred),
            }
        return {
            "attributes": dict(self.attributes),
            "children_sha256": list(self.child_digests),
            "kind": "operation",
            "operation": self.operation.value,
            "schema": NORMALIZATION_EXPRESSION_SCHEMA,
            "value": _semantic_metadata(self.value, self.inferred),
        }

    def _compute_semantic_digest(self) -> str:
        if self.collapsed_identity:
            return self.child_digests[0]
        return sha256_hex(canonical_json_bytes(self._semantic_record_unchecked()))

    def _canonical_record_unchecked(self) -> dict[str, object]:
        return {
            "attributes": dict(self.attributes),
            "children_sha256": list(self.child_digests),
            "collapsed_identity": self.collapsed_identity,
            "inferred": self.inferred.canonical_record(),
            "input_value_ids": list(self.input_value_ids),
            "logical_roots": list(self.logical_roots),
            "node_id": self.node_id,
            "operation": None if self.operation is None else self.operation.value,
            "semantic_sha256": self._semantic_digest,
            "value": self.value.canonical_record(),
            "value_id": self.value_id,
        }

    def _compute_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self._canonical_record_unchecked()))

    def validate(self) -> None:
        self._validate_structure()
        semantic_digest = getattr(self, "_semantic_digest", None)
        if (
            type(semantic_digest) is not str
            or SHA256_HEX.fullmatch(semantic_digest) is None
            or not hmac.compare_digest(
                semantic_digest,
                self._compute_semantic_digest(),
            )
        ):
            raise LineageError("lineage semantic digest does not match its contents")
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise LineageError("lineage expression digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise LineageError("lineage expression digest does not match its contents")

    @property
    def semantic_digest(self) -> str:
        self.validate()
        return self._semantic_digest

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


@dataclass(frozen=True, slots=True)
class NormalizationSite:
    """One qualifying divide site with semantic and diagnostic identities."""

    node_id: str
    value_id: str
    expression_digest: str
    logical_roots: tuple[str, ...]
    logical_outputs: tuple[str, ...]
    _site_digest: str = field(init=False, repr=False)
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(self, "_site_digest", self._compute_site_digest())
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not NormalizationSite:
            raise LineageError("normalization site must be an exact NormalizationSite")
        _require_identifier(self.node_id, label="normalization node identifier")
        _require_identifier(self.value_id, label="normalization value identifier")
        _require_digest(
            self.expression_digest,
            label="normalization expression digest",
        )
        for label, identifiers, limit in (
            (
                "normalization logical roots",
                self.logical_roots,
                MAX_GRAPH_INPUTS,
            ),
            (
                "normalization logical outputs",
                self.logical_outputs,
                MAX_GRAPH_OUTPUTS,
            ),
        ):
            if type(identifiers) is not tuple:
                raise LineageError(f"{label} must be a tuple")
            for identifier in identifiers:
                _require_identifier(
                    identifier,
                    label=label,
                    max_length=MAX_CONTRACT_ID_LENGTH,
                )
            if len(identifiers) > limit:
                raise LineageError(f"{label} exceed the graph interface limit")
            if not identifiers or list(identifiers) != sorted(set(identifiers)):
                raise LineageError(f"{label} must be nonempty and sorted")

    def _semantic_record_unchecked(self) -> dict[str, object]:
        return {
            "expression_sha256": self.expression_digest,
            "logical_outputs": list(self.logical_outputs),
            "logical_roots": list(self.logical_roots),
            "schema": NORMALIZATION_SITE_SCHEMA,
        }

    def _compute_site_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self._semantic_record_unchecked()))

    def _canonical_record_unchecked(self) -> dict[str, object]:
        return {
            **self._semantic_record_unchecked(),
            "node_id": self.node_id,
            "site_sha256": self._site_digest,
            "value_id": self.value_id,
        }

    def _compute_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self._canonical_record_unchecked()))

    def validate(self) -> None:
        self._validate_structure()
        site_digest = getattr(self, "_site_digest", None)
        if (
            type(site_digest) is not str
            or SHA256_HEX.fullmatch(site_digest) is None
            or not hmac.compare_digest(site_digest, self._compute_site_digest())
        ):
            raise LineageError(
                "normalization site digest does not match its semantic contents"
            )
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise LineageError("normalization diagnostic digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise LineageError(
                "normalization diagnostic digest does not match its contents"
            )

    @property
    def site_digest(self) -> str:
        self.validate()
        return self._site_digest

    @property
    def digest(self) -> str:
        self.validate()
        return self._digest

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return self._canonical_record_unchecked()


@dataclass(frozen=True, slots=True)
class OutputLineage:
    """One logical public output and its normalization-site digest multiset."""

    contract_id: str
    value_id: str
    position: int
    expression_digest: str
    site_digests: tuple[str, ...]
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not OutputLineage:
            raise LineageError("output lineage must be an exact OutputLineage")
        _require_identifier(
            self.contract_id,
            label="output logical contract identifier",
            max_length=MAX_CONTRACT_ID_LENGTH,
        )
        _require_identifier(self.value_id, label="output value identifier")
        if type(self.position) is not int:
            raise LineageError("output position must be an exact integer")
        if self.position < 0 or self.position >= MAX_GRAPH_OUTPUTS:
            raise LineageError("output position is out of bounds")
        _require_digest(self.expression_digest, label="output expression digest")
        if type(self.site_digests) is not tuple:
            raise LineageError("output site digests must be a tuple")
        if len(self.site_digests) > MAX_GRAPH_NODES:
            raise LineageError("output site digests exceed the graph node limit")
        for digest in self.site_digests:
            _require_digest(digest, label="output site digest")
        if list(self.site_digests) != sorted(self.site_digests):
            raise LineageError("output site digest multiset must be sorted")

    def _canonical_record_unchecked(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "expression_sha256": self.expression_digest,
            "position": self.position,
            "site_sha256_multiset": _digest_multiset_record(self.site_digests),
            "value_id": self.value_id,
        }

    def _compute_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self._canonical_record_unchecked()))

    def validate(self) -> None:
        self._validate_structure()
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise LineageError("output lineage digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise LineageError("output lineage digest does not match its contents")

    @property
    def digest(self) -> str:
        self.validate()
        return self._digest

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return self._canonical_record_unchecked()


@dataclass(frozen=True, slots=True)
class NormalizationLineage:
    """Unsigned lineage claim produced from one plan-scoped graph side.

    Direct construction does not prove that ``verification_result`` was run
    freshly. :func:`extract_normalization_lineage` validates its bindings,
    complete coverage, and semantic replay before constructing this record.
    """

    side: LineageSide
    comparison_id: str
    plan_digest: str
    graph_digest: str
    registry_digest: str
    limits: SolverLimits
    verification_result: VerificationResult
    expressions: tuple[LineageExpression, ...]
    sites: tuple[NormalizationSite, ...]
    outputs: tuple[OutputLineage, ...]
    _semantic_digest: str = field(init=False, repr=False)
    _digest: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._validate_structure()
        object.__setattr__(
            self,
            "_semantic_digest",
            self._compute_semantic_digest(),
        )
        object.__setattr__(self, "_digest", self._compute_digest())
        self.validate()

    def _validate_structure(self) -> None:
        if type(self) is not NormalizationLineage:
            raise LineageError(
                "normalization lineage must be an exact NormalizationLineage"
            )
        if type(self.side) is not LineageSide:
            raise LineageError("normalization lineage side is unsupported")
        _require_identifier(
            self.comparison_id,
            label="lineage comparison identifier",
            max_length=MAX_COMPARISON_ID_LENGTH,
        )
        for label, digest in (
            ("lineage plan digest", self.plan_digest),
            ("lineage graph digest", self.graph_digest),
            ("lineage registry digest", self.registry_digest),
        ):
            _require_digest(digest, label=label)
        if type(self.limits) is not SolverLimits:
            raise LineageError("lineage limits must be an exact SolverLimits")
        if type(self.verification_result) is not VerificationResult:
            raise LineageError(
                "lineage verification must be an exact VerificationResult"
            )
        try:
            self.limits.validate()
            self.verification_result.validate()
        except UnitSentinelError:
            raise LineageError(
                "lineage verification metadata is malformed or mutated"
            ) from None
        if (
            self.verification_result.status is not VerificationStatus.VERIFIED
            or self.verification_result.graph_digest != self.graph_digest
            or self.verification_result.registry_digest != self.registry_digest
            or canonical_json_bytes(self.verification_result.limits.canonical_record())
            != canonical_json_bytes(self.limits.canonical_record())
        ):
            raise LineageError("lineage verification bindings are inconsistent")
        self._validate_expressions()
        self._validate_sites_and_outputs()

    def _validate_expressions(self) -> None:
        if type(self.expressions) is not tuple:
            raise LineageError("lineage expressions must be a tuple")
        if not self.expressions or len(self.expressions) > MAX_GRAPH_VALUES:
            raise LineageError("lineage expression count is out of bounds")
        seen: dict[str, LineageExpression] = {}
        node_ids: set[str] = set()
        root_ids: set[str] = set()
        input_count = 0
        operation_count = 0
        saw_operation = False
        for expression in self.expressions:
            if type(expression) is not LineageExpression:
                raise LineageError(
                    "lineage expressions must be exact LineageExpression values"
                )
            expression.validate()
            if expression.value_id in seen:
                raise LineageError("lineage value identifiers must be unique")
            if expression.operation is None:
                input_count += 1
                if input_count > MAX_GRAPH_INPUTS:
                    raise LineageError("lineage contains too many input expressions")
                if saw_operation:
                    raise LineageError(
                        "lineage inputs must precede operation expressions"
                    )
                root_id = expression.logical_roots[0]
                if root_id in root_ids:
                    raise LineageError("lineage input roots must be unique")
                root_ids.add(root_id)
            else:
                operation_count += 1
                if operation_count > MAX_GRAPH_NODES:
                    raise LineageError(
                        "lineage contains too many operation expressions"
                    )
                saw_operation = True
                assert expression.node_id is not None
                if expression.node_id in node_ids:
                    raise LineageError("lineage node identifiers must be unique")
                node_ids.add(expression.node_id)
                if any(input_id not in seen for input_id in expression.input_value_ids):
                    raise LineageError(
                        "lineage inputs must reference earlier expressions"
                    )
                expected_children = tuple(
                    seen[input_id].semantic_digest
                    for input_id in expression.input_value_ids
                )
                if expression.operation in _COMMUTATIVE_OPERATIONS:
                    expected_children = tuple(sorted(expected_children))
                if expression.child_digests != expected_children:
                    raise LineageError(
                        "lineage child digests do not match diagnostic inputs"
                    )
                expected_roots = tuple(
                    sorted(
                        {
                            root
                            for input_id in expression.input_value_ids
                            for root in seen[input_id].logical_roots
                        }
                    )
                )
                if expression.logical_roots != expected_roots:
                    raise LineageError("lineage logical roots do not match its DAG")
                input_expression = seen[expression.input_value_ids[0]]
                must_collapse = (
                    expression.operation is Operation.IDENTITY
                    and _metadata_equal(
                        input_expression.value,
                        input_expression.inferred,
                        expression.value,
                        expression.inferred,
                    )
                )
                if expression.collapsed_identity is not must_collapse:
                    raise LineageError(
                        "identity collapse flag does not match exact metadata"
                    )
            seen[expression.value_id] = expression

        contracts = {
            contract.value_id: contract
            for contract in self.verification_result.contracts
        }
        contract_ids = tuple(contracts)
        if tuple(sorted(seen)) != contract_ids:
            raise LineageError("lineage expressions do not cover every verified value")
        if any(
            expression.inferred != contracts[value_id]
            for value_id, expression in seen.items()
        ):
            raise LineageError(
                "lineage inferred metadata does not match the verified contracts"
            )

    def _validate_sites_and_outputs(self) -> None:
        if type(self.sites) is not tuple or len(self.sites) > MAX_GRAPH_NODES:
            raise LineageError("normalization sites must be a bounded tuple")
        if (
            type(self.outputs) is not tuple
            or not self.outputs
            or len(self.outputs) > MAX_GRAPH_OUTPUTS
        ):
            raise LineageError("output lineages must be a nonempty bounded tuple")
        expressions = {item.value_id: item for item in self.expressions}
        qualifying = {
            item.value_id
            for item in self.expressions
            if _is_normalization_expression(item)
        }
        site_values: set[str] = set()
        site_nodes: set[str] = set()
        for site in self.sites:
            if type(site) is not NormalizationSite:
                raise LineageError(
                    "normalization sites must be exact NormalizationSite values"
                )
            site.validate()
            expression = expressions.get(site.value_id)
            if (
                expression is None
                or expression.node_id != site.node_id
                or expression.semantic_digest != site.expression_digest
                or expression.logical_roots != site.logical_roots
                or not _is_normalization_expression(expression)
            ):
                raise LineageError("normalization site does not match its expression")
            if site.value_id in site_values or site.node_id in site_nodes:
                raise LineageError("normalization diagnostic identities must be unique")
            site_values.add(site.value_id)
            site_nodes.add(site.node_id)
        if site_values != qualifying:
            raise LineageError(
                "normalization sites do not cover every qualifying divide"
            )

        contract_ids: list[str] = []
        output_values: set[str] = set()
        positions: set[int] = set()
        for output in self.outputs:
            if type(output) is not OutputLineage:
                raise LineageError("output lineages must be exact OutputLineage values")
            output.validate()
            expression = expressions.get(output.value_id)
            if (
                expression is None
                or output.expression_digest != expression.semantic_digest
            ):
                raise LineageError("output lineage does not match its expression")
            contract_ids.append(output.contract_id)
            if output.value_id in output_values or output.position in positions:
                raise LineageError("output lineage occurrences must be unique")
            output_values.add(output.value_id)
            positions.add(output.position)
        if contract_ids != sorted(set(contract_ids)):
            raise LineageError("output contract identifiers must be sorted and unique")
        if positions != set(range(len(self.outputs))):
            raise LineageError("output positions must be complete")

        reachable = _reachable_outputs(self.expressions, self.outputs)
        if any(not output_ids for output_ids in reachable.values()):
            raise LineageError("every lineage expression must reach a logical output")
        output_contracts = set(contract_ids)
        for site in self.sites:
            if set(
                site.logical_outputs
            ) - output_contracts or site.logical_outputs != tuple(
                sorted(reachable[site.value_id])
            ):
                raise LineageError("normalization site output routing is inconsistent")
        for output in self.outputs:
            expected = tuple(
                sorted(
                    site.site_digest
                    for site in self.sites
                    if output.contract_id in site.logical_outputs
                )
            )
            if output.site_digests != expected:
                raise LineageError("output normalization-site multiset is inconsistent")

    def _semantic_record_unchecked(self) -> dict[str, object]:
        return {
            "schema": NORMALIZATION_LINEAGE_SEMANTIC_SCHEMA,
            "site_sha256_multiset": _digest_multiset_record(
                tuple(sorted(site.site_digest for site in self.sites))
            ),
        }

    def _compute_semantic_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self._semantic_record_unchecked()))

    def _canonical_record_unchecked(self) -> dict[str, object]:
        return {
            "authentication": LINEAGE_AUTHENTICATION,
            "comparison_id": self.comparison_id,
            "expressions": [
                {
                    "record": expression.canonical_record(),
                    "sha256": expression.digest,
                }
                for expression in self.expressions
            ],
            "graph_sha256": self.graph_digest,
            "limits": self.limits.canonical_record(),
            "outputs": [
                {"record": output.canonical_record(), "sha256": output.digest}
                for output in self.outputs
            ],
            "plan_sha256": self.plan_digest,
            "registry_sha256": self.registry_digest,
            "schema": NORMALIZATION_LINEAGE_SCHEMA,
            "semantic_sha256": self._semantic_digest,
            "side": self.side.value,
            "sites": [
                {"record": site.canonical_record(), "sha256": site.digest}
                for site in self.sites
            ],
            "verification": {
                "record": self.verification_result.canonical_record(),
                "sha256": self.verification_result.digest,
            },
        }

    def _compute_digest(self) -> str:
        return sha256_hex(canonical_json_bytes(self._canonical_record_unchecked()))

    def validate(self) -> None:
        self._validate_structure()
        semantic_digest = getattr(self, "_semantic_digest", None)
        if (
            type(semantic_digest) is not str
            or SHA256_HEX.fullmatch(semantic_digest) is None
            or not hmac.compare_digest(
                semantic_digest,
                self._compute_semantic_digest(),
            )
        ):
            raise LineageError(
                "normalization lineage semantic digest does not match its contents"
            )
        digest = getattr(self, "_digest", None)
        if type(digest) is not str or SHA256_HEX.fullmatch(digest) is None:
            raise LineageError("normalization lineage digest is malformed")
        if not hmac.compare_digest(digest, self._compute_digest()):
            raise LineageError(
                "normalization lineage digest does not match its contents"
            )

    @property
    def semantic_digest(self) -> str:
        self.validate()
        return self._semantic_digest

    @property
    def digest(self) -> str:
        self.validate()
        return self._digest

    def output_site_digest_multiset(self, contract_id: str) -> tuple[str, ...]:
        self.validate()
        lookup = _require_identifier(
            contract_id,
            label="output contract lookup",
            max_length=MAX_CONTRACT_ID_LENGTH,
        )
        for output in self.outputs:
            if output.contract_id == lookup:
                return output.site_digests
        raise LineageError("output contract is not present in this lineage")

    def canonical_record(self) -> dict[str, object]:
        self.validate()
        return self._canonical_record_unchecked()

    def canonical_bytes(self) -> bytes:
        self.validate()
        return canonical_json_bytes(self._canonical_record_unchecked())


def _is_normalization_expression(expression: LineageExpression) -> bool:
    return (
        expression.operation is Operation.DIVIDE
        and expression.inferred.dimension == DIMENSIONLESS
        and expression.inferred.kind is QuantityKind.LINEAR
    )


def _reachable_outputs(
    expressions: tuple[LineageExpression, ...],
    outputs: tuple[OutputLineage, ...],
) -> dict[str, set[str]]:
    reachable: dict[str, set[str]] = {
        expression.value_id: set() for expression in expressions
    }
    for output in outputs:
        reachable[output.value_id].add(output.contract_id)
    for expression in reversed(expressions):
        if expression.operation is None:
            continue
        routed = reachable[expression.value_id]
        for input_id in expression.input_value_ids:
            reachable[input_id].update(routed)
    return reachable


@dataclass(frozen=True, slots=True)
class _LineagePins:
    plan_digest: str
    plan_bytes: bytes
    graph_digest: str
    graph_bytes: bytes
    registry_digest: str
    registry_bytes: bytes
    limits_bytes: bytes
    policy_bytes: bytes
    verification_digest: str
    verification_bytes: bytes
    solver_version: str


def extract_normalization_lineage(
    plan: ComparisonPlan,
    *,
    side: LineageSide,
    graph: ComputationGraph,
    registry: UnitRegistry = BUILTIN_REGISTRY,
    verification_result: VerificationResult,
    limits: SolverLimits = _DEFAULT_LINEAGE_LIMITS,
    policy: ComparisonPolicy = _DEFAULT_LINEAGE_POLICY,
) -> NormalizationLineage:
    """Derive a bounded DAG from one supplied verified-result claim.

    Identity, complete coverage, and semantic replay are checked here. They do
    not prove that the supplied result was produced freshly or authenticate
    its uniqueness claim.
    """

    (
        plan,
        side,
        graph,
        registry,
        verification_result,
        limits,
        policy,
    ) = _validate_lineage_inputs(
        plan,
        side,
        graph,
        registry,
        verification_result,
        limits,
        policy,
    )
    pins = _pin_lineage_inputs(
        plan,
        graph,
        registry,
        verification_result,
        limits,
        policy,
    )
    _validate_source_bindings(plan, side, pins)
    input_contracts, output_contracts = _interface_contracts(plan, side, graph)
    _validate_verified_result(
        verification_result,
        graph=graph,
        registry=registry,
        limits=limits,
        pins=pins,
    )
    _require_lineage_inputs_unchanged(
        plan,
        graph,
        registry,
        verification_result,
        limits,
        policy,
        pins,
    )

    contracts = {
        contract.value_id: contract for contract in verification_result.contracts
    }
    expressions, expressions_by_value = _build_expressions(
        graph,
        contracts,
        input_contracts,
    )
    output_routes = _route_outputs(graph, output_contracts)
    sites = _build_sites(graph, expressions_by_value, output_routes)
    outputs = _build_outputs(
        graph,
        output_contracts,
        expressions_by_value,
        sites,
    )
    _require_lineage_inputs_unchanged(
        plan,
        graph,
        registry,
        verification_result,
        limits,
        policy,
        pins,
    )
    lineage = NormalizationLineage(
        side=side,
        comparison_id=plan.comparison_id,
        plan_digest=pins.plan_digest,
        graph_digest=pins.graph_digest,
        registry_digest=pins.registry_digest,
        limits=limits,
        verification_result=verification_result,
        expressions=expressions,
        sites=sites,
        outputs=outputs,
    )
    _require_lineage_inputs_unchanged(
        plan,
        graph,
        registry,
        verification_result,
        limits,
        policy,
        pins,
    )
    lineage.validate()
    return lineage


def _validate_lineage_inputs(
    plan: object,
    side: object,
    graph: object,
    registry: object,
    verification_result: object,
    limits: object,
    policy: object,
) -> tuple[
    ComparisonPlan,
    LineageSide,
    ComputationGraph,
    UnitRegistry,
    VerificationResult,
    SolverLimits,
    ComparisonPolicy,
]:
    if type(plan) is not ComparisonPlan:
        raise LineageError("lineage requires an exact ComparisonPlan")
    if type(side) is not LineageSide:
        raise LineageError("lineage side must be an exact LineageSide")
    if type(graph) is not ComputationGraph:
        raise LineageError("lineage graph must be an exact ComputationGraph")
    if type(registry) is not UnitRegistry:
        raise LineageError("lineage registry must be an exact UnitRegistry")
    if type(verification_result) is not VerificationResult:
        raise LineageError("lineage verification must be an exact VerificationResult")
    if type(limits) is not SolverLimits:
        raise LineageError("lineage limits must be an exact SolverLimits")
    if type(policy) is not ComparisonPolicy:
        raise LineageError("lineage policy must be an exact ComparisonPolicy")
    try:
        policy.validate()
    except UnitSentinelError:
        raise LineageError("lineage policy is malformed or mutated") from None
    _require_expected_plan_digest(plan, policy)
    try:
        plan.validate()
        graph.validate()
        registry.validate()
        verification_result.validate()
        limits.validate()
    except UnitSentinelError:
        raise LineageError("lineage inputs are malformed or mutated") from None
    return plan, side, graph, registry, verification_result, limits, policy


def _require_expected_plan_digest(
    plan: ComparisonPlan,
    policy: ComparisonPolicy,
) -> None:
    expected = policy.expected_plan_digest
    if expected is None:
        return
    stored = getattr(plan, "_digest", None)
    if type(stored) is not str or not hmac.compare_digest(expected, stored):
        raise LineageError("lineage plan does not match the caller-trusted digest pin")


def _pin_lineage_inputs(
    plan: ComparisonPlan,
    graph: ComputationGraph,
    registry: UnitRegistry,
    verification_result: VerificationResult,
    limits: SolverLimits,
    policy: ComparisonPolicy,
) -> _LineagePins:
    try:
        solver_version = z3.get_version_string()
        if (
            type(solver_version) is not str
            or SOLVER_VERSION.fullmatch(solver_version) is None
        ):
            raise LineageError("current solver version is malformed")
        return _LineagePins(
            plan_digest=plan.digest,
            plan_bytes=plan.canonical_bytes(),
            graph_digest=graph.digest,
            graph_bytes=graph.canonical_bytes(),
            registry_digest=registry.digest,
            registry_bytes=registry.canonical_bytes(),
            limits_bytes=canonical_json_bytes(limits.canonical_record()),
            policy_bytes=canonical_json_bytes(policy.canonical_record()),
            verification_digest=verification_result.digest,
            verification_bytes=verification_result.canonical_bytes(),
            solver_version=solver_version,
        )
    except Exception:
        raise LineageError("lineage inputs could not be pinned") from None


def _validate_source_bindings(
    plan: ComparisonPlan,
    side: LineageSide,
    pins: _LineagePins,
) -> None:
    planned_graph = (
        plan.training_graph_digest
        if side is LineageSide.TRAINING
        else plan.serving_graph_digest
    )
    if not hmac.compare_digest(planned_graph, pins.graph_digest):
        raise LineageError("lineage graph does not match the selected plan side")
    if not hmac.compare_digest(plan.registry_digest, pins.registry_digest):
        raise LineageError("lineage registry does not match the comparison plan")


def _interface_contracts(
    plan: ComparisonPlan,
    side: LineageSide,
    graph: ComputationGraph,
) -> tuple[dict[str, str], dict[str, str]]:
    expected = {
        *((InterfaceRole.INPUT, value_id) for value_id in graph.inputs),
        *((InterfaceRole.OUTPUT, value_id) for value_id in graph.outputs),
    }
    covered: set[tuple[InterfaceRole, str]] = set()
    inputs: dict[str, str] = {}
    outputs: dict[str, str] = {}
    for binding in plan.bindings:
        endpoint = binding.training if side is LineageSide.TRAINING else binding.serving
        if endpoint is None:
            continue
        occurrence = (endpoint.role, endpoint.value_id)
        if occurrence not in expected:
            raise LineageError("lineage plan endpoint is not a public graph occurrence")
        covered.add(occurrence)
        target = inputs if endpoint.role is InterfaceRole.INPUT else outputs
        target[endpoint.value_id] = binding.contract_id
    if covered != expected:
        raise LineageError(
            "lineage plan must cover every public occurrence exactly once"
        )
    return inputs, outputs


def _validate_verified_result(
    result: VerificationResult,
    *,
    graph: ComputationGraph,
    registry: UnitRegistry,
    limits: SolverLimits,
    pins: _LineagePins,
) -> None:
    if (
        result.status is not VerificationStatus.VERIFIED
        or result.graph_digest != pins.graph_digest
        or result.registry_digest != pins.registry_digest
        or result.solver_version != pins.solver_version
        or canonical_json_bytes(result.limits.canonical_record())
        != canonical_json_bytes(limits.canonical_record())
    ):
        raise LineageError("lineage verification identity is inconsistent")
    expected = tuple(value.value_id for value in graph.values)
    if tuple(contract.value_id for contract in result.contracts) != expected:
        raise LineageError("lineage verification does not cover every graph value")
    try:
        replayed = _replay_claimed_contracts(graph, registry, result.contracts)
        result.validate()
    except Exception:
        raise LineageError(
            "lineage verification contracts could not be replayed"
        ) from None
    if replayed is not True:
        raise LineageError("lineage verification contracts failed semantic replay")


def _require_lineage_inputs_unchanged(
    plan: ComparisonPlan,
    graph: ComputationGraph,
    registry: UnitRegistry,
    verification_result: VerificationResult,
    limits: SolverLimits,
    policy: ComparisonPolicy,
    pins: _LineagePins,
) -> None:
    try:
        plan.validate()
        graph.validate()
        registry.validate()
        verification_result.validate()
        limits.validate()
        policy.validate()
        unchanged = (
            plan.digest == pins.plan_digest
            and plan.canonical_bytes() == pins.plan_bytes
            and graph.digest == pins.graph_digest
            and graph.canonical_bytes() == pins.graph_bytes
            and registry.digest == pins.registry_digest
            and registry.canonical_bytes() == pins.registry_bytes
            and verification_result.digest == pins.verification_digest
            and verification_result.canonical_bytes() == pins.verification_bytes
            and canonical_json_bytes(limits.canonical_record()) == pins.limits_bytes
            and canonical_json_bytes(policy.canonical_record()) == pins.policy_bytes
        )
    except Exception:
        raise LineageError("lineage inputs changed during extraction") from None
    if not unchanged:
        raise LineageError("lineage inputs changed during extraction")


def _node_attributes(node: Node) -> tuple[tuple[str, str], ...]:
    if node.operation is Operation.POWER:
        assert node.exponent is not None
        return (("exponent", _fraction_text(node.exponent)),)
    if node.operation is Operation.CONVERT:
        assert node.target_unit_id is not None
        return (("unit_id", node.target_unit_id),)
    return ()


def _build_expressions(
    graph: ComputationGraph,
    contracts: dict[str, InferredContract],
    input_contracts: dict[str, str],
) -> tuple[
    tuple[LineageExpression, ...],
    dict[str, LineageExpression],
]:
    expressions: list[LineageExpression] = []
    by_value: dict[str, LineageExpression] = {}
    values_by_id = {value.value_id: value for value in graph.values}
    for value_id in graph.inputs:
        expression = LineageExpression(
            value_id=value_id,
            node_id=None,
            operation=None,
            attributes=(),
            input_value_ids=(),
            child_digests=(),
            logical_roots=(input_contracts[value_id],),
            collapsed_identity=False,
            value=values_by_id[value_id],
            inferred=contracts[value_id],
        )
        expressions.append(expression)
        by_value[value_id] = expression

    for node in graph.nodes:
        input_expressions = tuple(by_value[value_id] for value_id in node.inputs)
        child_digests = tuple(item.semantic_digest for item in input_expressions)
        if node.operation in _COMMUTATIVE_OPERATIONS:
            child_digests = tuple(sorted(child_digests))
        output_value = values_by_id[node.output]
        output_contract = contracts[node.output]
        collapsed = node.operation is Operation.IDENTITY and _metadata_equal(
            input_expressions[0].value,
            input_expressions[0].inferred,
            output_value,
            output_contract,
        )
        expression = LineageExpression(
            value_id=node.output,
            node_id=node.node_id,
            operation=node.operation,
            attributes=_node_attributes(node),
            input_value_ids=node.inputs,
            child_digests=child_digests,
            logical_roots=tuple(
                sorted(
                    {
                        root
                        for input_expression in input_expressions
                        for root in input_expression.logical_roots
                    }
                )
            ),
            collapsed_identity=collapsed,
            value=output_value,
            inferred=output_contract,
        )
        expressions.append(expression)
        by_value[node.output] = expression
    return tuple(expressions), by_value


def _route_outputs(
    graph: ComputationGraph,
    output_contracts: dict[str, str],
) -> dict[str, set[str]]:
    reachable: dict[str, set[str]] = {value.value_id: set() for value in graph.values}
    for value_id in graph.outputs:
        reachable[value_id].add(output_contracts[value_id])
    for node in reversed(graph.nodes):
        routed = reachable[node.output]
        for input_id in node.inputs:
            reachable[input_id].update(routed)
    return reachable


def _build_sites(
    graph: ComputationGraph,
    expressions: dict[str, LineageExpression],
    output_routes: dict[str, set[str]],
) -> tuple[NormalizationSite, ...]:
    sites: list[NormalizationSite] = []
    for node in graph.nodes:
        expression = expressions[node.output]
        if not _is_normalization_expression(expression):
            continue
        sites.append(
            NormalizationSite(
                node_id=node.node_id,
                value_id=node.output,
                expression_digest=expression.semantic_digest,
                logical_roots=expression.logical_roots,
                logical_outputs=tuple(sorted(output_routes[node.output])),
            )
        )
    return tuple(sites)


def _build_outputs(
    graph: ComputationGraph,
    output_contracts: dict[str, str],
    expressions: dict[str, LineageExpression],
    sites: tuple[NormalizationSite, ...],
) -> tuple[OutputLineage, ...]:
    outputs = tuple(
        OutputLineage(
            contract_id=output_contracts[value_id],
            value_id=value_id,
            position=position,
            expression_digest=expressions[value_id].semantic_digest,
            site_digests=tuple(
                sorted(
                    site.site_digest
                    for site in sites
                    if output_contracts[value_id] in site.logical_outputs
                )
            ),
        )
        for position, value_id in enumerate(graph.outputs)
    )
    return tuple(sorted(outputs, key=lambda item: item.contract_id))
