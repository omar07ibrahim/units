# Training-serving comparison contract v1

Status: the canonical comparison plan, bounded plan byte codec,
fresh-verification engine, immutable comparison result, bounded
normalization-lineage extractor, and fresh cross-graph normalization-lineage
comparison are implemented. The strict result codec, CLI surface, and
reproducible comparison evidence remain future work.

## Objective

A training graph and a serving graph can each be dimensionally verified while
their public contracts still disagree. Version 1 defines an explicit,
content-addressed plan for aligning those interfaces without guessing from
names, positions, tensor values, or statistical samples.

The plan is not a compatibility result. It binds the exact graph and registry
bytes that the comparison engine verifies and names every intended public
contract slot.

## Closed plan model

`ComparisonPlan` contains:

| Field | Contract |
| --- | --- |
| `comparison_id` | Canonical lowercase identifier for this comparison |
| `training_graph_digest` | SHA-256 of one canonical training graph |
| `serving_graph_digest` | SHA-256 of one canonical serving graph |
| `registry_digest` | SHA-256 of the one registry snapshot used on both sides |
| `bindings` | One to 256 sorted, unique logical contract bindings |

The serialized schema is
`unitsentinel.training-serving-comparison/v1`. The plan digest is the SHA-256
of its canonical bytes and is deliberately not embedded in those bytes.

Each `ContractBinding` has one canonical `contract_id` and two nullable sides:

```json
{
  "contract_id": "current-wheel-speed",
  "serving": {
    "role": "input",
    "value_id": "request-speed"
  },
  "training": {
    "role": "input",
    "value_id": "wheel-speed-feature"
  }
}
```

An endpoint names an intended public occurrence `(role, value_id)`, where
`role` is `input` or `output`. Constructing or decoding the plan does not prove
that the value exists, that the declared role is public, or that every public
occurrence is covered. The engine establishes all three properties from the
bound graphs and rejects internal values.

## Explicit alignment only

The plan never performs fuzzy, positional, alias-based, or embedding-based
matching. Different graph identifiers may be aligned only through one explicit
logical binding.

A two-sided binding declares different `value_id` spellings to be an
intentional rename only under that caller-trusted plan; the rename alone is not
drift. The plan itself grants no permission. The result is therefore
always scoped as “compatible under this plan”, binds the exact plan SHA-256,
and reports `authentication: not-provided`. Port order remains independent
metadata and is compared separately.

The invariants are:

- bindings are sorted and unique by `contract_id`;
- the same endpoint occurrence cannot appear twice on one side;
- at least one side of every binding is present;
- a two-sided binding may retain different roles so the engine can report an
  explicit role drift rather than silently repairing it;
- a training-only binding declares a serving-side absence;
- a serving-only binding declares a training-side absence.

The comparison engine additionally establishes endpoint membership and
requires every public input and output occurrence from both bound graphs to
appear exactly once. An omitted occurrence is an incomplete plan, not
permission to ignore part of an interface.

This representation distinguishes three cases that a same-name join cannot:

1. an intentional rename with otherwise matching semantics;
2. a public value missing on one side;
3. a value moved from input to output or the reverse.

## Byte boundary

The decoder accepts exact canonical UTF-8 JSON bytes and reuses UnitSentinel's
allocation preflight. Version 1 fixes:

- 131,072 document bytes;
- six nested containers;
- 256 entries in one object or array;
- 4,096 total JSON values;
- 192 Unicode scalar values in one string;
- ten digits in an integer token.

Duplicate fields, floats, non-finite numbers, a BOM, invalid UTF-8, unknown or
missing fields, noncanonical ordering or whitespace, oversized structures, and
unsupported schemas fail closed. Decoding reconstructs the immutable model and
then requires its canonical bytes to equal the input byte for byte.

## Fresh comparison engine

`compare_graphs` accepts the plan plus keyword-only training and serving
graphs, one registry snapshot, one exact `SolverLimits`, and an optional
`ComparisonPolicy` with a caller-trusted expected plan digest. It evaluates
them in this order:

1. validate exact plan, graph, registry, limit, and caller policy object types;
2. when policy supplies an expected plan digest, require it to match before
   interpreting any binding;
3. require both graph digests and the registry digest to match the plan;
4. establish endpoint membership and require total, duplicate-free coverage of
   every public occurrence;
5. freshly run the existing verifier for both graphs with the same registry
   snapshot and bounded solver policy;
6. compare no interfaces unless both results are complete `verified` results;
7. extract both source-bound normalization lineages, recheck pinned inputs and
   accepted verifier results, then independently rederive each full lineage
   from its real graph before using either candidate;
8. compare each aligned endpoint's role, dtype, declared shape, explicit unit
   identifier, dimension, quantity kind, exact scale, and exact offset;
9. compare the counted routed normalization-site multiset for every two-sided
   output binding;
10. revalidate all nested objects and identities before publishing a result;
11. emit `authentication: not-provided` and the exact plan digest in every
   report.

A conflict, underconstrained graph, unknown verification, timeout, resource
limit, stale digest, incomplete contract coverage, unexpected verifier return,
or nested mutation can never become a positive compatibility result.

For ordinary verifier outcomes the engine makes exactly two verifier calls,
even when the training result is already negative or indeterminate. A source
mutation is a rejected comparison input and can stop processing immediately.
Each accepted positive result must cover every graph value and pass the
solver-independent semantic replay before any interface snapshot is built.

The immutable result uses schema
`unitsentinel.training-serving-comparison-result/v1` and has three outcomes:

| Status | Meaning |
| --- | --- |
| `compatible` | Both freshly verify; mapped fields and output digests agree |
| `drift` | Both graphs freshly verify and at least one exact mismatch is present |
| `indeterminate` | A graph or lineage is not accepted; no partial findings |

Each decisive binding lists mismatch codes in this fixed order:
`missing-in-serving`, `extra-in-serving`, `role-drift`, `position-drift`,
`dtype-drift`, `shape-drift`, `explicit-unit-drift`, `dimension-drift`,
`kind-drift`, `scale-drift`, `offset-drift`, and
`normalization-lineage-drift`. The lineage code is possible only for a
two-sided output and is always last. Position is role-local: input and output
ordinals are not compared across a role change. Different `value_id` strings
are not themselves drift because the explicit binding already declares the
logical alignment.

The work is structurally bounded by 256 bindings, the graph size limits, and
two verifier calls. `SolverLimits.total_timeout_ms` and `max_memory_mb` apply
to each solver call; the documented worst-case solver time is therefore twice
the per-call total plus bounded validation and replay overhead.

`ComparisonResult` is an unsigned detached claim. Direct construction cannot
prove freshness or rerun graph-backed lineage derivation because the detached
record does not carry either graph. Callers that rely on a result must obtain
it from `compare_graphs`, retain the caller-trusted plan policy separately, and
treat its content digest as integrity rather than author authentication.

## Implemented lineage extraction and comparison boundary

Version 1 normalization provenance is intentionally narrower than arbitrary
graph equivalence. `extract_normalization_lineage` traces verified,
dimensionless, linear ratio-normalization sites created by `divide`
operations. It accepts one plan-scoped graph side and a supplied positive
verification claim, then checks exact source bindings, complete contract
coverage, current solver identity, and independent semantic replay before
deriving lineage. This establishes consistency with the supplied claim, not
that the verifier was invoked freshly; the integrated comparison engine
supplies that freshness boundary.

The lineage fingerprint:

- use mapped logical contract IDs for public input roots;
- include closed operation attributes, dtype, shape, explicit unit annotation,
  and exact inferred dimension, kind, scale, and offset;
- collapse an identity node only when all of that metadata is unchanged;
- sort children only for operations that are actually commutative;
- preserve order for subtract, divide, and matrix multiplication;
- ignore internal node/value renames;
- bind each site to its sorted reachable logical public outputs;
- preserve repeated identical sites as a counted multiset;
- separate the rename-invariant semantic digest from source diagnostics and
  provenance;
- bind every canonical record by SHA-256 without recursively expanding the
  bounded expression DAG.

The standalone output can establish a deterministic lineage for one accepted
claim. The fresh engine integrates two such claims only after accepting both
verifier results. It revalidates source bindings, complete logical input/output
mappings and positions, boundary metadata, routed output aggregates, and the
first lineage again after extracting the second. Before accepting an extracted
artifact, it independently rederives the complete canonical lineage—including
every internal expression, operation attribute, site, route, and output—from
the bound plan, graph, accepted verification result, and limits, then requires
identical digest and canonical bytes. A difference does not establish that
training is correct, serving is wrong, or two different algebraic forms are
mathematically equivalent.

### Canonical lineage records

The outer artifact schema is
`unitsentinel.normalization-lineage/v1`. It binds:

- `comparison_id`, selected `side`, plan, graph, and registry SHA-256 values;
- the exact solver limits and supplied verification record plus its digest;
- a topologically ordered table of diagnostic expression records;
- every qualifying site and every logical public output record;
- `authentication: not-provided`; and
- one rename-invariant `semantic_sha256`.

Expression semantics use
`unitsentinel.normalization-expression/v1`. An input record contains its
mapped logical contract ID and normalized value metadata. An operation record
contains only the closed operation, canonical `power` exponent or `convert`
target where applicable, child expression digests, and normalized output
metadata. Graph-local node and value IDs appear only in diagnostic records.
A site uses `unitsentinel.normalization-site/v1` and binds the expression
digest, sorted logical roots, and sorted reachable logical outputs. The bundle
semantic record is
`unitsentinel.normalization-lineage-semantic/v1` and contains a sorted counted
multiset of site digests, so two identical site occurrences are not collapsed.

Output records retain the public position and expression digest for review and
store their routed site digests as a sorted multiset. Each output also stores
`normalization_sha256`, a domain-separated digest of its logical contract ID
and counted routed site multiset under
`unitsentinel.output-normalization/v1`. The engine compares this digest only
for two-sided output bindings. Output position and diagnostic source IDs do
not enter the bundle semantic digest.

The outer comparison result stores each accepted lineage at
`normalization_lineage.training` or `normalization_lineage.serving` as
`{"record": ..., "sha256": ...}`. Each two-sided output binding contains
`normalization.training_sha256` and `normalization.serving_sha256`; other
binding shapes contain `normalization: null`.

The construction is iterative. It computes each expression once in
topological order and propagates logical outputs in reverse topological order;
it never serializes a recursive expression tree or enumerates downstream
paths. Existing graph limits bound it to 64 inputs, 512 nodes, 64 outputs, 576
values, 512 sites, 1,024 graph edges, and at most 32,768 site/output
associations.

Direct dataclass construction is an unsigned claim and cannot prove graph
provenance or verifier freshness. The public extractor additionally rejects
wrong side or registry bindings, incomplete public mappings, non-positive or
incomplete verification claims, failed semantic replay, noncanonical records,
and mutation of the pinned plan, graph, registry, limits, policy, or supplied
result during extraction.

The integrated engine calls both extractors in deterministic training-then-
serving order after both verifications succeed. While pinned comparison inputs
remain unchanged, an extractor exception, unexpected value, wrong source,
missing or swapped public mapping, or changed lineage evidence makes the result
`indeterminate` with
`reason: normalization-lineage-failure`. Both still-valid verification results
are retained; both canonical lineage fields are `null`, and `bindings` is
empty. Mutation of the pinned plan, graph, registry, limits, or policy remains
an input error rather than a reportable drift.

## Security and nonclaims

The plan contains no tensor payloads, model code, import paths, URLs, plugins,
callbacks, credentials, or executable metadata. Comparing it requires no
network and does not run a model.

The plan and report are unsigned content-addressed records. Their digests
detect byte changes; they do not authenticate an author, prove that a
deployment used either graph, validate broadcasting or matrix shapes, measure
statistical drift, or establish scientific correctness.

A party that needs an approved mapping must supply a caller-trusted expected
plan digest or equivalent allow-list policy to the engine and must reject a
mismatch. Recomputing a digest after replacing a plan establishes only the
integrity of the replacement; it does not make the replacement trusted.
